// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

// Package capture runs tcpdump for the controller's packet capture job (ADR 0194).
//
// It is not a general sniffer. The filter is built here from structured fields (an interface, a
// protocol, one IP address and one port), never accepted as text, tcpdump is started without a
// shell with a fixed argument list, a capture is bounded in time, packets and bytes, and only one
// runs at a time.
package capture

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/netip"
	"os"
	"os/exec"
	"regexp"
	"strconv"
	"strings"
	"sync"
	"time"
)

// Limits of one capture.
const (
	MaxDuration   = 60 * time.Second
	MaxPackets    = 20000
	MinSnaplen    = 64
	MaxSnaplen    = 262144
	DefaultMaxOut = 16 << 20 // bytes of pcap sent back before the capture is cut short
	chunkSize     = 64 << 10
)

var (
	ifaceName     = regexp.MustCompile(`^[A-Za-z0-9_.:-]{1,15}$`)
	packetsCaught = regexp.MustCompile(`(\d+) packets? captured`)
	errBusy       = errors.New("another capture is already running on this engine")
)

// Request is one capture.
type Request struct {
	Interface  string // "any" or an interface of this engine
	Protocol   string // "", tcp, udp or icmp
	Host       string // optional IP address
	Port       uint32 // optional, with "", tcp or udp
	Duration   time.Duration
	MaxPackets uint32
	Snaplen    uint32 // 0 means MaxSnaplen
}

// Result is what a finished capture reports.
type Result struct {
	Packets   uint64
	Truncated bool
}

// Starter starts the capture process: it returns the pcap stream, a function that returns what the
// process wrote to its error output, and a function that waits for it to end.
type Starter func(ctx context.Context, args []string) (io.ReadCloser, func() string, func() error, error)

// Capturer validates and runs captures, one at a time.
type Capturer struct {
	// Interfaces lists the interface names of this engine (net.Interfaces by default).
	Interfaces func() ([]string, error)
	// Start starts tcpdump (the real one by default).
	Start Starter
	// MaxBytes caps the pcap bytes sent back (DefaultMaxOut by default).
	MaxBytes int

	mu sync.Mutex
}

// New returns a Capturer that runs tcpdump.
func New() *Capturer {
	return &Capturer{Interfaces: systemInterfaces, Start: startTcpdump}
}

func systemInterfaces() ([]string, error) {
	all, err := net.Interfaces()
	if err != nil {
		return nil, err
	}
	names := make([]string, 0, len(all))
	for _, i := range all {
		names = append(names, i.Name)
	}
	return names, nil
}

// Validate checks a request against the limits and returns what is wrong with it.
func (c *Capturer) Validate(r Request) error {
	if r.Interface != "any" {
		if !ifaceName.MatchString(r.Interface) {
			return fmt.Errorf("interface %q is not a valid interface name", r.Interface)
		}
		names, err := c.Interfaces()
		if err != nil {
			return fmt.Errorf("cannot list interfaces: %v", err)
		}
		found := false
		for _, n := range names {
			found = found || n == r.Interface
		}
		if !found {
			return fmt.Errorf("this engine has no interface %q", r.Interface)
		}
	}
	switch r.Protocol {
	case "", "tcp", "udp", "icmp":
	default:
		return fmt.Errorf("protocol %q is not one of tcp, udp, icmp", r.Protocol)
	}
	if r.Host != "" {
		if _, err := netip.ParseAddr(r.Host); err != nil || strings.Contains(r.Host, "%") {
			return fmt.Errorf("host %q is not an IP address", r.Host)
		}
	}
	if r.Port > 65535 {
		return fmt.Errorf("port %d is out of range", r.Port)
	}
	if r.Port != 0 && r.Protocol == "icmp" {
		return errors.New("a port cannot be combined with icmp")
	}
	if r.Duration < time.Second || r.Duration > MaxDuration {
		return fmt.Errorf("duration must be 1 to %d seconds", int(MaxDuration/time.Second))
	}
	if r.MaxPackets < 1 || r.MaxPackets > MaxPackets {
		return fmt.Errorf("max_packets must be 1 to %d", MaxPackets)
	}
	if r.Snaplen != 0 && (r.Snaplen < MinSnaplen || r.Snaplen > MaxSnaplen) {
		return fmt.Errorf("snaplen must be %d to %d", MinSnaplen, MaxSnaplen)
	}
	return nil
}

// Args returns the tcpdump argument list for a validated request. The filter expression comes after
// "--", so nothing in it can be read as an option.
func Args(r Request) []string {
	snap := r.Snaplen
	if snap == 0 {
		snap = MaxSnaplen
	}
	args := []string{
		"-n", "-p", "-U", "-i", r.Interface,
		"-s", strconv.FormatUint(uint64(snap), 10),
		"-c", strconv.FormatUint(uint64(r.MaxPackets), 10),
		"-w", "-", "--",
	}
	var clauses [][]string
	switch r.Protocol {
	case "tcp", "udp":
		clauses = append(clauses, []string{r.Protocol})
	case "icmp":
		clauses = append(clauses, []string{"(", "icmp", "or", "icmp6", ")"})
	}
	if r.Host != "" {
		clauses = append(clauses, []string{"host", r.Host})
	}
	if r.Port != 0 {
		clauses = append(clauses, []string{"port", strconv.FormatUint(uint64(r.Port), 10)})
	}
	for i, c := range clauses {
		if i > 0 {
			args = append(args, "and")
		}
		args = append(args, c...)
	}
	return args
}

// Run validates the request, runs one capture and calls send with the pcap bytes in order. A
// capture cut short by the byte cap is not an error: it sets Result.Truncated.
func (c *Capturer) Run(ctx context.Context, r Request, send func([]byte) error) (Result, error) {
	if err := c.Validate(r); err != nil {
		return Result{}, err
	}
	if !c.mu.TryLock() {
		return Result{}, errBusy
	}
	defer c.mu.Unlock()
	ctx, cancel := context.WithTimeout(ctx, r.Duration)
	defer cancel()
	out, stderr, wait, err := c.Start(ctx, Args(r))
	if err != nil {
		return Result{}, fmt.Errorf("cannot start tcpdump: %v", err)
	}
	limit := c.MaxBytes
	if limit == 0 {
		limit = DefaultMaxOut
	}
	var res Result
	sent := 0
	buf := make([]byte, chunkSize)
	for {
		n, rerr := out.Read(buf)
		if n > 0 {
			if sent+n > limit {
				n = limit - sent
				res.Truncated = true
			}
			if n > 0 {
				if serr := send(append([]byte(nil), buf[:n]...)); serr != nil {
					cancel()
					_ = out.Close()
					_ = wait()
					return res, serr
				}
				sent += n
			}
			if res.Truncated {
				cancel()
				break
			}
		}
		if rerr != nil {
			break
		}
	}
	_ = out.Close()
	werr := wait()
	if m := packetsCaught.FindStringSubmatch(stderr()); m != nil {
		res.Packets, _ = strconv.ParseUint(m[1], 10, 64)
	}
	// A capture stopped by its deadline or the byte cap ends with a signal; that is the normal end.
	if werr != nil && ctx.Err() == nil && !res.Truncated {
		return res, fmt.Errorf("tcpdump failed: %s", firstLine(stderr(), werr))
	}
	return res, nil
}

func firstLine(stderr string, err error) string {
	for _, l := range strings.Split(stderr, "\n") {
		if l = strings.TrimSpace(l); l != "" {
			return l
		}
	}
	return err.Error()
}

// startTcpdump runs the real tcpdump. At the deadline it gets SIGINT, so it flushes and exits.
func startTcpdump(ctx context.Context, args []string) (io.ReadCloser, func() string, func() error, error) {
	cmd := exec.CommandContext(ctx, "tcpdump", args...) // #nosec G204 - fixed binary, built arguments
	cmd.Cancel = func() error { return cmd.Process.Signal(os.Interrupt) }
	cmd.WaitDelay = 3 * time.Second
	var errBuf bytes.Buffer
	cmd.Stderr = &errBuf
	out, err := cmd.StdoutPipe()
	if err != nil {
		return nil, nil, nil, err
	}
	if err := cmd.Start(); err != nil {
		return nil, nil, nil, err
	}
	return out, errBuf.String, cmd.Wait, nil
}

// IsBusy reports whether err means another capture is running.
func IsBusy(err error) bool { return errors.Is(err, errBusy) }
