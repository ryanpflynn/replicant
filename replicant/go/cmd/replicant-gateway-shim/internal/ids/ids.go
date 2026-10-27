// Package ids mints the prefixed ULIDs of C-IDS (e.g. gwu_<ulid>), lower-case Crockford base32.
package ids

import (
	"crypto/rand"
	"crypto/sha256"
	"encoding/binary"
	"sync"
	"time"
)

const alphabet = "0123456789abcdefghjkmnpqrstvwxyz"

var (
	mu       sync.Mutex
	lastMs   uint64
	lastRand [10]byte
)

// ULID returns a 26-char lower-case ULID, monotonic within a millisecond.
func ULID() string {
	mu.Lock()
	defer mu.Unlock()
	ms := uint64(time.Now().UnixMilli())
	if ms == lastMs {
		// increment the 80-bit random part
		for i := 9; i >= 0; i-- {
			lastRand[i]++
			if lastRand[i] != 0 {
				break
			}
		}
	} else {
		lastMs = ms
		_, _ = rand.Read(lastRand[:])
	}
	return encode(ms, lastRand)
}

// Derived returns a deterministic ULID-shaped string from a seed (used for scripted message and tool ids).
func Derived(seed string) string {
	h := sha256.Sum256([]byte(seed))
	var r [10]byte
	copy(r[:], h[6:16])
	return encode(binary.BigEndian.Uint64(append([]byte{0, 0}, h[0:6]...)), r)
}

func encode(ms uint64, r [10]byte) string {
	var b [16]byte
	b[0] = byte(ms >> 40)
	b[1] = byte(ms >> 32)
	b[2] = byte(ms >> 24)
	b[3] = byte(ms >> 16)
	b[4] = byte(ms >> 8)
	b[5] = byte(ms)
	copy(b[6:], r[:])
	// 128 bits -> 26 base32 chars (first char carries 3 bits)
	out := make([]byte, 26)
	hi := binary.BigEndian.Uint64(b[0:8])
	lo := binary.BigEndian.Uint64(b[8:16])
	for i := 25; i >= 0; i-- {
		out[i] = alphabet[lo&31]
		lo = (lo >> 5) | (hi << 59)
		hi >>= 5
	}
	return string(out)
}

// New returns prefix + "_" + ULID.
func New(prefix string) string { return prefix + "_" + ULID() }
