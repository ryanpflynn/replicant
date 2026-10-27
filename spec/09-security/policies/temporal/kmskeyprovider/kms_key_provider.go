// Package kmskeyprovider is the Go KeyProvider for the tenant-routed Temporal payload codec
// (R1-214, XD-01-08). The only Go Temporal client is run-notifier (exec account), whose role may call
// kms:GenerateDataKey only (C-ARCH 2.0.0). This provider therefore encrypts and never decrypts.
// Tenant keys live in the core account; KMS resolves a bare alias only in the caller's account, so the
// KeyId is always the alias ARN arn:aws:kms:<region>:<core account>:alias/replicant/<env>/tenant/<t>
// (R1V-004; matches 02-execution/runner-service.md payload-codec row).
// The wire framing (metadata keys, AAD) is WS-01's codec (ADR-01-010); this file supplies the keys.
package kmskeyprovider

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"crypto/rand"
	"errors"
	"fmt"
	"regexp"
	"sync"
	"time"

	"github.com/aws/aws-sdk-go-v2/service/kms"
	"github.com/aws/aws-sdk-go-v2/service/kms/types"
)

const purpose = "temporal-payload"

// ErrDecryptForbidden is returned by Decrypt: execution-side processes never decode payloads.
var ErrDecryptForbidden = errors.New("kmskeyprovider: decrypt is not permitted in the exec account")

type dataKey struct {
	plaintext []byte
	wrapped   []byte
	created   time.Time
	uses      int
}

// GenerateDataKeyAPI is the one KMS call this provider makes (*kms.Client satisfies it).
type GenerateDataKeyAPI interface {
	GenerateDataKey(ctx context.Context, in *kms.GenerateDataKeyInput, optFns ...func(*kms.Options)) (*kms.GenerateDataKeyOutput, error)
}

var (
	accountRe = regexp.MustCompile(`^[0-9]{12}$`)
	tenantRe  = regexp.MustCompile(`^[a-z][a-z0-9]*(-[a-z0-9]+)*$`) // C-IDS 2.0.0 tenant_id, 3..32 chars
)

// EncryptOnlyKeyProvider caches one data key per tenant for at most TTL and MaxUses payloads.
type EncryptOnlyKeyProvider struct {
	Env         string
	Region      string // region of the core tenant keys, e.g. us-east-1
	CoreAccount string // core account that owns the tenant keys, e.g. 111111111111
	KMS         GenerateDataKeyAPI
	TTL         time.Duration
	MaxUses     int

	mu   sync.Mutex
	keys map[string]*dataKey
}

func encryptionContext(tenant string) map[string]string {
	return map[string]string{"replicant:purpose": purpose, "replicant:tenant": tenant}
}

// KeyID returns the cross-account alias ARN for a tenant's key.
func (p *EncryptOnlyKeyProvider) KeyID(tenant string) (string, error) {
	if !accountRe.MatchString(p.CoreAccount) || p.Region == "" || p.Env == "" {
		return "", errors.New("kmskeyprovider: Region, CoreAccount and Env are required")
	}
	if len(tenant) < 3 || len(tenant) > 32 || !tenantRe.MatchString(tenant) {
		return "", fmt.Errorf("kmskeyprovider: invalid tenant %q", tenant)
	}
	return fmt.Sprintf("arn:aws:kms:%s:%s:alias/replicant/%s/tenant/%s", p.Region, p.CoreAccount, p.Env, tenant), nil
}

func (p *EncryptOnlyKeyProvider) key(ctx context.Context, tenant string) (*dataKey, error) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.keys == nil {
		p.keys = map[string]*dataKey{}
	}
	k := p.keys[tenant]
	if k != nil && time.Since(k.created) <= p.TTL && k.uses < p.MaxUses {
		k.uses++
		return k, nil
	}
	if k != nil {
		for i := range k.plaintext {
			k.plaintext[i] = 0
		}
	}
	keyID, err := p.KeyID(tenant)
	if err != nil {
		return nil, err
	}
	out, err := p.KMS.GenerateDataKey(ctx, &kms.GenerateDataKeyInput{
		KeyId:             strPtr(keyID),
		KeySpec:           types.DataKeySpecAes256,
		EncryptionContext: encryptionContext(tenant),
	})
	if err != nil {
		return nil, fmt.Errorf("kmskeyprovider: GenerateDataKey for tenant %q: %w", tenant, err)
	}
	k = &dataKey{plaintext: out.Plaintext, wrapped: out.CiphertextBlob, created: time.Now(), uses: 1}
	p.keys[tenant] = k
	return k, nil
}

// Encrypt returns (key_blob, nonce||ciphertext), matching WS-01's KeyProvider.encrypt.
func (p *EncryptOnlyKeyProvider) Encrypt(ctx context.Context, tenant string, plaintext, aad []byte) ([]byte, []byte, error) {
	k, err := p.key(ctx, tenant)
	if err != nil {
		return nil, nil, err
	}
	block, err := aes.NewCipher(k.plaintext)
	if err != nil {
		return nil, nil, err
	}
	gcm, err := cipher.NewGCM(block)
	if err != nil {
		return nil, nil, err
	}
	nonce := make([]byte, gcm.NonceSize())
	if _, err := rand.Read(nonce); err != nil {
		return nil, nil, err
	}
	return k.wrapped, gcm.Seal(nonce, nonce, plaintext, aad), nil
}

// Decrypt always fails in the exec account.
func (p *EncryptOnlyKeyProvider) Decrypt(context.Context, string, []byte, []byte, []byte) ([]byte, error) {
	return nil, ErrDecryptForbidden
}

func strPtr(s string) *string { return &s }
