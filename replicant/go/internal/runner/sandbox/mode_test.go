package sandbox

import (
	"os"
	"path/filepath"
	"testing"
)

func TestFileModeSpecialBits(t *testing.T) {
	for unix, want := range map[uint32]os.FileMode{
		0o755:  0o755,
		0o2775: os.ModeSetgid | 0o775,
		0o1777: os.ModeSticky | 0o777,
		0o4755: os.ModeSetuid | 0o755,
	} {
		if got := FileMode(unix); got != want {
			t.Errorf("FileMode(%o) = %v, want %v", unix, got, want)
		}
	}
	// os.Chmod with the converted mode really sets them (a raw 0o2775 would not)
	for _, unix := range []uint32{0o2775, 0o1777} {
		d := filepath.Join(t.TempDir(), "d")
		if err := os.Mkdir(d, 0o700); err != nil {
			t.Fatal(err)
		}
		if err := os.Chmod(d, FileMode(unix)); err != nil {
			t.Fatal(err)
		}
		fi, _ := os.Stat(d)
		if got := fi.Mode() &^ os.ModeDir; got != FileMode(unix) {
			t.Errorf("after chmod %o: %v", unix, got)
		}
	}
}
