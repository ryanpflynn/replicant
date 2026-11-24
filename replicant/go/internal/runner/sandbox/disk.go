package sandbox

import (
	"os"
	"path/filepath"
	"syscall"
)

// Workspace disk thresholds of the watchdog (local-runner.md §1.3), in percent of the limit.
const (
	DiskWarnPercent     = 85
	DiskExceededPercent = 98
)

// DiskUsage is the allocated size of the tree at root (hard links counted once), as du would report it.
func DiskUsage(root string) int64 {
	var total int64
	seen := map[uint64]bool{}
	_ = filepath.Walk(root, func(p string, fi os.FileInfo, err error) error {
		if err != nil {
			return nil
		}
		if st, ok := fi.Sys().(*syscall.Stat_t); ok {
			if st.Nlink > 1 {
				if seen[st.Ino] {
					return nil
				}
				seen[st.Ino] = true
			}
			total += st.Blocks * 512
		} else {
			total += fi.Size()
		}
		return nil
	})
	return total
}

// DiskLevel classifies usage against the limit: "ok", "warn" (≥ 85 %) or "exceeded" (≥ 98 %). No limit is "ok".
func DiskLevel(used, limit int64) string {
	switch {
	case limit <= 0:
		return "ok"
	case used*100 >= limit*DiskExceededPercent:
		return "exceeded"
	case used*100 >= limit*DiskWarnPercent:
		return "warn"
	}
	return "ok"
}
