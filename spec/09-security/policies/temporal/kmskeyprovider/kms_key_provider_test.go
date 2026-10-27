package kmskeyprovider

import (
	"context"
	"crypto/aes"
	"crypto/cipher"
	"errors"
	"testing"
	"time"

	"github.com/aws/aws-sdk-go-v2/service/kms"
)

type fakeKMS struct {
	keyIDs []string
	ctxs   []map[string]string
}

func (f *fakeKMS) GenerateDataKey(_ context.Context, in *kms.GenerateDataKeyInput, _ ...func(*kms.Options)) (*kms.GenerateDataKeyOutput, error) {
	f.keyIDs = append(f.keyIDs, *in.KeyId)
	f.ctxs = append(f.ctxs, in.EncryptionContext)
	pt := make([]byte, 32)
	for i := range pt {
		pt[i] = byte(len(f.keyIDs))
	}
	return &kms.GenerateDataKeyOutput{Plaintext: pt, CiphertextBlob: []byte("wrapped:" + *in.KeyId)}, nil
}

func newProvider(f *fakeKMS) *EncryptOnlyKeyProvider {
	return &EncryptOnlyKeyProvider{Env: "prod", Region: "us-east-1", CoreAccount: "111111111111", KMS: f, TTL: time.Minute, MaxUses: 2}
}

// R1V-004: the exec-account caller must name the core key by alias ARN, never by bare alias.
func TestKeyIDIsCrossAccountAliasARN(t *testing.T) {
	f := &fakeKMS{}
	p := newProvider(f)
	if _, _, err := p.Encrypt(context.Background(), "payments", []byte("x"), []byte("aad")); err != nil {
		t.Fatal(err)
	}
	want := "arn:aws:kms:us-east-1:111111111111:alias/replicant/prod/tenant/payments"
	if len(f.keyIDs) != 1 || f.keyIDs[0] != want {
		t.Fatalf("KeyId = %v, want %s", f.keyIDs, want)
	}
	if f.ctxs[0]["replicant:purpose"] != "temporal-payload" || f.ctxs[0]["replicant:tenant"] != "payments" {
		t.Fatalf("encryption context = %v", f.ctxs[0])
	}
	if _, err := p.KeyID("replicant-system"); err != nil {
		t.Fatalf("replicant-system must resolve: %v", err)
	}
}

func TestRejectsBadConfigAndTenant(t *testing.T) {
	p := newProvider(&fakeKMS{})
	for _, tn := range []string{"", "ab", "Payments", "a/../b", "payments-", "x:y"} {
		if _, err := p.KeyID(tn); err == nil {
			t.Errorf("tenant %q accepted", tn)
		}
	}
	p.CoreAccount = ""
	if _, err := p.KeyID("payments"); err == nil {
		t.Error("missing CoreAccount accepted")
	}
}

func TestCacheIsPerTenantAndBoundedByUses(t *testing.T) {
	f := &fakeKMS{}
	p := newProvider(f)
	ctx := context.Background()
	for i := 0; i < 3; i++ {
		if _, _, err := p.Encrypt(ctx, "payments", []byte("x"), nil); err != nil {
			t.Fatal(err)
		}
	}
	if _, _, err := p.Encrypt(ctx, "search", []byte("x"), nil); err != nil {
		t.Fatal(err)
	}
	if len(f.keyIDs) != 3 { // payments twice (MaxUses 2), search once
		t.Fatalf("GenerateDataKey calls = %d, want 3: %v", len(f.keyIDs), f.keyIDs)
	}
}

func TestCiphertextOpensWithDataKeyAndDecryptForbidden(t *testing.T) {
	f := &fakeKMS{}
	p := newProvider(f)
	blob, ct, err := p.Encrypt(context.Background(), "payments", []byte("hello"), []byte("aad"))
	if err != nil {
		t.Fatal(err)
	}
	if string(blob) != "wrapped:arn:aws:kms:us-east-1:111111111111:alias/replicant/prod/tenant/payments" {
		t.Fatalf("blob = %s", blob)
	}
	key := make([]byte, 32)
	for i := range key {
		key[i] = 1
	}
	b, _ := aes.NewCipher(key)
	g, _ := cipher.NewGCM(b)
	pt, err := g.Open(nil, ct[:g.NonceSize()], ct[g.NonceSize():], []byte("aad"))
	if err != nil || string(pt) != "hello" {
		t.Fatalf("open: %q %v", pt, err)
	}
	if _, err := p.Decrypt(context.Background(), "payments", blob, ct, nil); !errors.Is(err, ErrDecryptForbidden) {
		t.Fatalf("Decrypt err = %v", err)
	}
}
