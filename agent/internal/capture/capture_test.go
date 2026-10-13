// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

package capture

import (
	"bytes"
	"context"
	"errors"
	"io"
	"strings"
	"testing"
	"time"
)

func valid() Request {
	return Request{Interface: "any", Duration: 5 * time.Second, MaxPackets: 100}
}

func capturer(stream string, stderr string, waitErr error) *Capturer {
	return &Capturer{
		Interfaces: func() ([]string, error) { return []string{"lo", "eth0", "eth1"}, nil },
		Start: func(ctx context.Context, args []string) (io.ReadCloser, func() string, func() error, error) {
			return io.NopCloser(strings.NewReader(stream)), func() string { return stderr }, func() error { return waitErr }, nil
		},
	}
}

func TestValidateRefusesWhatIsNotStructured(t *testing.T) {
	c := capturer("", "", nil)
	bad := map[string]func(*Request){
		"interface with a shell character": func(r *Request) { r.Interface = "eth0;reboot" },
		"unknown interface":                func(r *Request) { r.Interface = "eth9" },
		"option as interface":              func(r *Request) { r.Interface = "-w/etc/x" },
		"unknown protocol":                 func(r *Request) { r.Protocol = "ip proto 1 or ether" },
		"host that is a filter":            func(r *Request) { r.Host = "1.2.3.4 or host 5.6.7.8" },
		"host with a zone":                 func(r *Request) { r.Host = "fe80::1%eth0" },
		"host name":                        func(r *Request) { r.Host = "example.test" },
		"port out of range":                func(r *Request) { r.Port = 70000 },
		"port with icmp":                   func(r *Request) { r.Protocol = "icmp"; r.Port = 80 },
		"no duration":                      func(r *Request) { r.Duration = 0 },
		"too long":                         func(r *Request) { r.Duration = 61 * time.Second },
		"no packets":                       func(r *Request) { r.MaxPackets = 0 },
		"too many packets":                 func(r *Request) { r.MaxPackets = MaxPackets + 1 },
		"tiny snaplen":                     func(r *Request) { r.Snaplen = 10 },
		"huge snaplen":                     func(r *Request) { r.Snaplen = MaxSnaplen + 1 },
	}
	for name, mutate := range bad {
		r := valid()
		mutate(&r)
		if err := c.Validate(r); err == nil {
			t.Errorf("%s was accepted", name)
		}
	}
	for _, ok := range []Request{
		valid(),
		{Interface: "eth0", Protocol: "tcp", Host: "192.0.2.10", Port: 443, Duration: time.Second, MaxPackets: 1, Snaplen: 96},
		{Interface: "lo", Protocol: "icmp", Host: "2001:db8::1", Duration: 60 * time.Second, MaxPackets: MaxPackets},
	} {
		if err := c.Validate(ok); err != nil {
			t.Errorf("%+v refused: %v", ok, err)
		}
	}
}

func TestArgsBuildTheFilterFromFieldsAfterTheOptionTerminator(t *testing.T) {
	got := strings.Join(Args(Request{
		Interface: "eth0", Protocol: "tcp", Host: "192.0.2.10", Port: 443, MaxPackets: 50, Snaplen: 128,
	}), " ")
	want := "-n -p -U -i eth0 -s 128 -c 50 -w - -- tcp and host 192.0.2.10 and port 443"
	if got != want {
		t.Fatalf("got  %s\nwant %s", got, want)
	}
	icmp := strings.Join(Args(Request{Interface: "any", Protocol: "icmp", MaxPackets: 1}), " ")
	if !strings.HasSuffix(icmp, "-- ( icmp or icmp6 )") || !strings.Contains(icmp, "-s 262144") {
		t.Fatalf("icmp args: %s", icmp)
	}
	none := Args(Request{Interface: "any", MaxPackets: 1})
	if none[len(none)-1] != "--" {
		t.Fatalf("no filter should end at the terminator: %v", none)
	}
}

func TestRunStreamsThePcapAndReportsThePacketCount(t *testing.T) {
	var got bytes.Buffer
	res, err := capturer("PCAPDATA", "tcpdump: listening on any\n12 packets captured\n12 packets received by filter\n", nil).
		Run(context.Background(), valid(), func(b []byte) error { got.Write(b); return nil })
	if err != nil || got.String() != "PCAPDATA" || res.Packets != 12 || res.Truncated {
		t.Fatalf("res=%+v err=%v data=%q", res, err, got.String())
	}
}

func TestRunStopsAtTheByteCapAndSaysSo(t *testing.T) {
	c := capturer(strings.Repeat("x", 1000), "3 packets captured\n", errors.New("signal: interrupt"))
	c.MaxBytes = 100
	var n int
	res, err := c.Run(context.Background(), valid(), func(b []byte) error { n += len(b); return nil })
	if err != nil || n != 100 || !res.Truncated {
		t.Fatalf("res=%+v err=%v sent=%d", res, err, n)
	}
}

func TestRunReportsATcpdumpFailureWithItsFirstErrorLine(t *testing.T) {
	_, err := capturer("", "\ntcpdump: eth1: No such device exists\n", errors.New("exit status 1")).
		Run(context.Background(), valid(), func([]byte) error { return nil })
	if err == nil || !strings.Contains(err.Error(), "No such device exists") {
		t.Fatalf("err=%v", err)
	}
}

func TestOnlyOneCaptureRunsAtATime(t *testing.T) {
	release := make(chan struct{})
	started := make(chan struct{})
	c := &Capturer{
		Interfaces: func() ([]string, error) { return nil, nil },
		Start: func(ctx context.Context, args []string) (io.ReadCloser, func() string, func() error, error) {
			close(started)
			pr, pw := io.Pipe()
			go func() { <-release; _ = pw.Close() }()
			return pr, func() string { return "" }, func() error { return nil }, nil
		},
	}
	done := make(chan error, 1)
	go func() {
		_, err := c.Run(context.Background(), valid(), func([]byte) error { return nil })
		done <- err
	}()
	<-started
	if _, err := c.Run(context.Background(), valid(), func([]byte) error { return nil }); !IsBusy(err) {
		t.Fatalf("second capture: %v", err)
	}
	close(release)
	if err := <-done; err != nil {
		t.Fatal(err)
	}
	// and the slot is free again afterwards
	c.Start = capturer("", "0 packets captured\n", nil).Start
	if _, err := c.Run(context.Background(), valid(), func([]byte) error { return nil }); err != nil {
		t.Fatalf("after release: %v", err)
	}
}

func TestASendFailureStopsTheCapture(t *testing.T) {
	boom := errors.New("client went away")
	_, err := capturer("abc", "", nil).Run(context.Background(), valid(), func([]byte) error { return boom })
	if !errors.Is(err, boom) {
		t.Fatalf("err=%v", err)
	}
}
