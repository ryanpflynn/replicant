package engine

import (
	"context"
	"net/http"
	"net/http/httptest"
	"testing"
)

// Remove must ask the engine to delete the container's anonymous volumes (v=true) as well as force it; without v
// every sandbox container leaked its anonymous volumes (FIX-D6).
func TestRemoveDeletesAnonymousVolumes(t *testing.T) {
	var got *http.Request
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		got = r
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()
	c, err := New(srv.URL)
	if err != nil {
		t.Fatal(err)
	}
	if err := c.Remove(context.Background(), "ar-x-agent"); err != nil {
		t.Fatal(err)
	}
	if got == nil || got.Method != http.MethodDelete || got.URL.Path != "/v1.41/containers/ar-x-agent" {
		t.Fatalf("unexpected request %+v", got)
	}
	q := got.URL.Query()
	if q.Get("force") != "true" || q.Get("v") != "true" {
		t.Fatalf("query = %q, want force=true&v=true", got.URL.RawQuery)
	}
}

func TestRemoveIgnoresNotFound(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		http.Error(w, `{"message":"no such container"}`, http.StatusNotFound)
	}))
	defer srv.Close()
	c, _ := New(srv.URL)
	if err := c.Remove(context.Background(), "gone"); err != nil {
		t.Fatalf("404 must be nil, got %v", err)
	}
}
