package sandbox

import (
	"os"
	"path/filepath"
	"testing"
)

func TestDiskLevel(t *testing.T) {
	for _, c := range []struct {
		used, limit int64
		want        string
	}{
		{100, 0, "ok"}, {84, 100, "ok"}, {85, 100, "warn"}, {97, 100, "warn"}, {98, 100, "exceeded"}, {150, 100, "exceeded"},
	} {
		if got := DiskLevel(c.used, c.limit); got != c.want {
			t.Errorf("DiskLevel(%d, %d) = %s, want %s", c.used, c.limit, got, c.want)
		}
	}
}

func TestDiskUsageCountsHardLinksOnce(t *testing.T) {
	d := t.TempDir()
	if err := os.WriteFile(filepath.Join(d, "a"), make([]byte, 64<<10), 0o644); err != nil {
		t.Fatal(err)
	}
	one := DiskUsage(d)
	if one < 64<<10 {
		t.Fatalf("usage %d < file size", one)
	}
	if err := os.Link(filepath.Join(d, "a"), filepath.Join(d, "b")); err != nil {
		t.Skip("hard links unsupported:", err)
	}
	if two := DiskUsage(d); two != one {
		t.Fatalf("hard link counted twice: %d != %d", two, one)
	}
}
