// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

package main

import (
	"crypto/x509"
	"crypto/x509/pkix"
	"testing"
)

func TestAllowedClientNames(t *testing.T) {
	leaf := &x509.Certificate{Subject: pkix.Name{CommonName: "controller"}, DNSNames: []string{"ctl.example.test"}}
	chains := [][]*x509.Certificate{{leaf}}
	cases := []struct {
		allowed []string
		ok      bool
	}{
		{nil, true}, // not configured: any certificate the CA signed
		{[]string{"controller"}, true},
		{[]string{"other", "ctl.example.test"}, true},
		{[]string{"se-2"}, false},
	}
	for _, c := range cases {
		err := allowedClient(c.allowed)(nil, chains)
		if (err == nil) != c.ok {
			t.Errorf("allowed %v: err = %v", c.allowed, err)
		}
	}
}

func TestSplitNamesAndMissingCertificate(t *testing.T) {
	if got := splitNames(" a, b ,,"); len(got) != 2 || got[0] != "a" || got[1] != "b" {
		t.Errorf("splitNames = %v", got)
	}
	if splitNames("") != nil {
		t.Error("an empty list must be nil")
	}
	if certCommonName("/does/not/exist") != "" {
		t.Error("a missing certificate has no name")
	}
}
