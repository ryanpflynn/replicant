package sandbox

import "os"

// FileMode converts a Unix permission number (e.g. 0o2775, 0o1777) to an os.FileMode. Go's os.Chmod ignores the
// raw setuid/setgid/sticky bits 0o4000/0o2000/0o1000; they must be passed as os.ModeSetuid/ModeSetgid/ModeSticky.
func FileMode(unix uint32) os.FileMode {
	m := os.FileMode(unix & 0o777)
	if unix&0o4000 != 0 {
		m |= os.ModeSetuid
	}
	if unix&0o2000 != 0 {
		m |= os.ModeSetgid
	}
	if unix&0o1000 != 0 {
		m |= os.ModeSticky
	}
	return m
}
