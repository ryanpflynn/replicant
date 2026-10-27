// Package ids is the Go mirror of the C-IDS 2.0.0 reference implementation (ids-and-naming.md §1):
// lowercase Crockford base32 bodies, derived IDs from SHA-256, ULIDs, and the job trace ID rule T-1.
package ids

import (
	"crypto/rand"
	"crypto/sha1"
	"crypto/sha256"
	"encoding/binary"
	"encoding/hex"
	"fmt"
	"math/big"
	"regexp"
	"strings"
	"sync"
	"time"
)

// Alphabet is lowercase Crockford base32 (no i, l, o, u).
const Alphabet = "0123456789abcdefghjkmnpqrstvwxyz"

var (
	RunIDRe       = regexp.MustCompile(`^run_[0-9a-hjkmnp-tv-z]{26}$`)
	JobIDRe       = regexp.MustCompile(`^job_[0-9a-hjkmnp-tv-z]{26}$`)
	TenantRe      = regexp.MustCompile(`^[a-z][a-z0-9]*(-[a-z0-9]+)*$`)
	LogicalActRe  = regexp.MustCompile(`^[a-z][a-z0-9_]{0,40}\.c[0-9]{1,3}\.n[0-9]{1,3}\.[a-z][a-z0-9_]{0,30}$`)
	IdemKeyRe     = regexp.MustCompile(`^ik1:[A-Za-z0-9_:.-]{1,251}$`)
	MessageIDRe   = regexp.MustCompile(`^msg_[0-9a-hjkmnp-tv-z]{26}$`)
	WorkflowIDRe  = regexp.MustCompile(`^job_[0-9a-hjkmnp-tv-z]{26}(:[a-z][a-z0-9_]{1,30}:[1-9][0-9]*)?$`)
	PrincipalIDRe = regexp.MustCompile(`^(usr_[0-9a-hjkmnp-tv-z]{26}|svc_[a-z][a-z0-9-]{2,40})$`)
)

// B32_130 renders the first 130 bits of digest as 26 base32 characters.
func B32_130(digest []byte) string {
	n := new(big.Int).SetBytes(digest[:17])
	n.Rsh(n, 136-130)
	return encode130(n)
}

func encode130(n *big.Int) string {
	var b [26]byte
	m := new(big.Int)
	thirtyOne := big.NewInt(31)
	for i := 0; i < 26; i++ {
		m.Rsh(n, uint(5*(25-i)))
		m.And(m, thirtyOne)
		b[i] = Alphabet[m.Int64()]
	}
	return string(b[:])
}

// Derived returns prefix_<b32_130(sha256(fields joined by \n))>.
func Derived(prefix string, fields ...string) string {
	d := sha256.Sum256([]byte(strings.Join(fields, "\n")))
	return prefix + "_" + B32_130(d[:])
}

var (
	ulidMu   sync.Mutex
	lastMS   int64
	lastRand [10]byte
)

// ULID returns prefix_<ulid>, monotonic within a millisecond in this process.
func ULID(prefix string) string {
	ulidMu.Lock()
	defer ulidMu.Unlock()
	ms := time.Now().UnixMilli()
	var r [10]byte
	if ms == lastMS {
		r = lastRand
		for i := 9; i >= 0; i-- { // increment the 80-bit random part
			r[i]++
			if r[i] != 0 {
				break
			}
		}
	} else {
		if _, err := rand.Read(r[:]); err != nil {
			panic(err)
		}
	}
	lastMS, lastRand = ms, r
	var buf [16]byte
	binary.BigEndian.PutUint16(buf[0:2], uint16(ms>>32))
	binary.BigEndian.PutUint32(buf[2:6], uint32(ms))
	copy(buf[6:], r[:])
	n := new(big.Int).SetBytes(buf[:])
	return prefix + "_" + encode130(n)
}

// Body returns the 26-character body of an ID like run_xxx.
func Body(id string) string {
	if i := strings.IndexByte(id, '_'); i >= 0 {
		return id[i+1:]
	}
	return id
}

// JobTraceID implements rule T-1.
func JobTraceID(jobID string) string {
	d := sha256.Sum256([]byte("replicant.job.trace.v1:" + jobID))
	return hex.EncodeToString(d[:])[:32]
}

// NamespaceURL is the RFC 4122 URL namespace.
var NamespaceURL = [16]byte{0x6b, 0xa7, 0xb8, 0x11, 0x9d, 0xad, 0x11, 0xd1, 0x80, 0xb4, 0x00, 0xc0, 0x4f, 0xd4, 0x30, 0xc8}

// UUID5 returns the RFC 4122 version-5 UUID of name in namespace ns.
func UUID5(ns [16]byte, name string) string {
	h := sha1.New()
	h.Write(ns[:])
	h.Write([]byte(name))
	s := h.Sum(nil)
	s[6] = (s[6] & 0x0f) | 0x50
	s[8] = (s[8] & 0x3f) | 0x80
	return fmt.Sprintf("%x-%x-%x-%x-%x", s[0:4], s[4:6], s[6:8], s[8:10], s[10:16])
}

// SessionID is the deterministic Claude Code session id for a run (cli-invocation.md §2.1).
func SessionID(runID string) string { return UUID5(NamespaceURL, "replicant:"+runID) }
