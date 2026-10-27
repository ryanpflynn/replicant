package main

import "testing"

func TestWants(t *testing.T) {
	body := []byte("0011command=fetch0001000dthin-pack0032want 12b0bcaffb2f7137e9fc20357c9359025e5ee8be\n0009done\n0000")
	w := wants(body)
	if len(w) != 1 || w[0] != "12b0bcaffb2f7137e9fc20357c9359025e5ee8be" {
		t.Fatalf("%v", w)
	}
	if !repoIDRe.MatchString("1") || repoIDRe.MatchString("../x") {
		t.Fatal("repo id pattern")
	}
}
