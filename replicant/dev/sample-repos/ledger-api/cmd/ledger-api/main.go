// Command ledger-api serves a tiny JSON API over the ledger package.
//
//	POST /v1/refunds  {"charge": {...}, "refund_minor": 4000}  ->  [entries]
package main

import (
	"encoding/json"
	"log"
	"net/http"
	"os"

	"github.com/acme-payments/ledger-api/internal/ledger"
)

type refundRequest struct {
	Charge      ledger.Charge `json:"charge"`
	RefundMinor int64         `json:"refund_minor"`
}

func main() {
	mux := http.NewServeMux()
	mux.HandleFunc("GET /healthz", func(w http.ResponseWriter, _ *http.Request) { w.WriteHeader(http.StatusNoContent) })
	mux.HandleFunc("POST /v1/refunds", func(w http.ResponseWriter, r *http.Request) {
		var req refundRequest
		if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
			http.Error(w, err.Error(), http.StatusBadRequest)
			return
		}
		entries, err := ledger.ApplyRefund(req.Charge, req.RefundMinor)
		if err != nil {
			http.Error(w, err.Error(), http.StatusUnprocessableEntity)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(entries)
	})
	addr := os.Getenv("LEDGER_API_ADDR")
	if addr == "" {
		addr = ":8080"
	}
	log.Printf("ledger-api listening on %s", addr)
	log.Fatal(http.ListenAndServe(addr, mux))
}
