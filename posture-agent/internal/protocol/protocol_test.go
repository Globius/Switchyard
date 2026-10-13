// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

package protocol

import (
	"context"
	"crypto/ed25519"
	"encoding/base64"
	"encoding/json"
	"io"
	"net/http"
	"net/http/httptest"
	"os"
	"strconv"
	"strings"
	"testing"
	"time"
)

type vector struct {
	Seed         string `json:"seed_b64"`
	PublicKey    string `json:"public_key_b64"`
	Method       string `json:"method"`
	Path         string `json:"path"`
	TimestampMS  int64  `json:"timestamp_ms"`
	Body         string `json:"body"`
	Canonical    string `json:"canonical"`
	SignatureB64 string `json:"signature_b64"`
}

func loadVector(t *testing.T) vector {
	t.Helper()
	raw, err := os.ReadFile("testdata/vector.json")
	if err != nil {
		t.Fatal(err)
	}
	var v vector
	if err := json.Unmarshal(raw, &v); err != nil {
		t.Fatal(err)
	}
	return v
}

// The controller's tests verify the same file with its own implementation.
func TestSignatureMatchesTheControllersVector(t *testing.T) {
	v := loadVector(t)
	seed, _ := base64.StdEncoding.DecodeString(v.Seed)
	key := ed25519.NewKeyFromSeed(seed)
	if got := base64.StdEncoding.EncodeToString(key.Public().(ed25519.PublicKey)); got != v.PublicKey {
		t.Fatalf("public key %s != %s", got, v.PublicKey)
	}
	if got := string(Canonical(v.Method, v.Path, v.TimestampMS, []byte(v.Body))); got != v.Canonical {
		t.Fatalf("canonical form differs:\n%q\n%q", got, v.Canonical)
	}
	if got := Sign(key, v.Method, v.Path, v.TimestampMS, []byte(v.Body)); got != v.SignatureB64 {
		t.Fatalf("signature %s != %s", got, v.SignatureB64)
	}
}

func TestCanonicalUppercasesTheMethodAndHashesTheBody(t *testing.T) {
	a := Canonical("post", "/p", 5, []byte("x"))
	b := Canonical("POST", "/p", 5, []byte("x"))
	if string(a) != string(b) || !strings.HasPrefix(string(a), "POST\n/p\n5\n") {
		t.Fatalf("%q", a)
	}
	if string(Canonical("POST", "/p", 5, []byte("y"))) == string(a) {
		t.Fatal("the body must be part of the signature")
	}
}

func TestClientRefusesPlainHTTPAndBadURLs(t *testing.T) {
	for _, bad := range []string{"http://c.example.test", "ftp://x", "not a url", "", "https://"} {
		if _, err := NewClient(bad, nil, false); err == nil {
			t.Errorf("%q should be refused", bad)
		}
	}
	if _, err := NewClient("http://127.0.0.1:8000", nil, true); err != nil {
		t.Fatal(err)
	}
	if _, err := NewClient("https://c.example.test/", nil, false); err != nil {
		t.Fatal(err)
	}
}

func TestEnrollAndReportTalkTheControllersLanguage(t *testing.T) {
	pub, priv, _ := ed25519.GenerateKey(nil)
	var seen *http.Request
	var seenBody []byte
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		seen = r
		seenBody, _ = io.ReadAll(r.Body)
		switch r.URL.Path {
		case "/base" + EnrollPath:
			io.WriteString(w, `{"device_id":"d1","tenant":"acme","name":"laptop","policy":"managed","cert_ttl_h":12}`)
		case "/base" + ReportPath:
			io.WriteString(w, `{"compliant":false,"failures":["firewall_off"],"certificate":"","ca":"","not_after":"","next_report_s":600}`)
		default:
			http.NotFound(w, r)
		}
	}))
	defer srv.Close()
	c, err := NewClient(srv.URL+"/base/", nil, true)
	if err != nil {
		t.Fatal(err)
	}
	c.Now = func() time.Time { return time.UnixMilli(1_760_000_000_000) }

	got, err := c.Enroll(context.Background(), "sydev_x", "laptop", pub, "ana")
	if err != nil || got.DeviceID != "d1" || got.CertTTLH != 12 {
		t.Fatalf("%+v %v", got, err)
	}
	var enrollBody map[string]string
	_ = json.Unmarshal(seenBody, &enrollBody)
	if enrollBody["token"] != "sydev_x" || enrollBody["public_key"] != base64.StdEncoding.EncodeToString(pub) || enrollBody["owner"] != "ana" {
		t.Fatalf("%v", enrollBody)
	}

	f := Facts{OS: "linux", OSVersion: "6.8.0", AgentVersion: AgentVersion}
	res, ts, err := c.SendReport(context.Background(), "d1", priv, Report{Facts: f, CSR: "csr"}, 0)
	if err != nil || res.Compliant || res.Failures[0] != "firewall_off" || res.NextReportS != 600 {
		t.Fatalf("%+v %v", res, err)
	}
	if ts != 1_760_000_000_000 || seen.Header.Get("X-Device-Timestamp") != strconv.FormatInt(ts, 10) || seen.Header.Get("X-Device-Id") != "d1" {
		t.Fatalf("headers: %v", seen.Header)
	}
	// the signature covers the path the server saw, including the base path of the URL
	sig, _ := base64.StdEncoding.DecodeString(seen.Header.Get("X-Device-Signature"))
	if !ed25519.Verify(pub, Canonical("POST", seen.URL.Path, ts, seenBody), sig) || seen.URL.Path != "/base"+ReportPath {
		t.Fatal("the signature does not cover the request as sent")
	}
	var doc map[string]json.RawMessage
	_ = json.Unmarshal(seenBody, &doc)
	if _, ok := doc["facts"]; !ok || string(doc["csr"]) != `"csr"` {
		t.Fatalf("body: %s", seenBody)
	}
	if strings.Contains(string(doc["facts"]), `"hostname"`) {
		t.Fatal("an unknown hostname is left out, not sent as null")
	}

	// timestamps rise even when the clock does not
	_, ts2, _ := c.SendReport(context.Background(), "d1", priv, Report{Facts: f}, ts)
	if ts2 != ts+1 {
		t.Fatalf("ts2 %d", ts2)
	}
}

func TestControllerErrorsAreReadFromTheProblemDocument(t *testing.T) {
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/problem+json")
		w.WriteHeader(401)
		io.WriteString(w, `{"title":"Unauthorized","detail":"invalid or expired token"}`)
	}))
	defer srv.Close()
	c, _ := NewClient(srv.URL, nil, true)
	pub, _, _ := ed25519.GenerateKey(nil)
	_, err := c.Enroll(context.Background(), "t", "n", pub, "")
	if err == nil || !strings.Contains(err.Error(), "401") || !strings.Contains(err.Error(), "invalid or expired token") {
		t.Fatalf("%v", err)
	}
}
