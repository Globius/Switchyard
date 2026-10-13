// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

package server

import (
	"context"
	"crypto/ed25519"
	"io"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"

	pb "switchyard/agent/gen/switchyard/agent/v1"
	"switchyard/agent/internal/bundle"
	"switchyard/agent/internal/capture"
	agentstatus "switchyard/agent/internal/status"
)

type okRunner struct{ bgp string }

func (r okRunner) Run(_ context.Context, name string, args ...string) ([]byte, error) {
	if name == "vtysh" {
		return []byte(r.bgp), nil
	}
	return nil, nil
}

func start(t *testing.T) (pb.AgentClient, ed25519.PrivateKey, string) {
	t.Helper()
	pub, priv, _ := ed25519.GenerateKey(nil)
	root := t.TempDir()
	state := t.TempDir()
	os.WriteFile(filepath.Join(state, "vrrp-edge.state"), []byte("master\n"), 0o600)
	run := okRunner{bgp: `{"ipv4Unicast":{"peers":{"10.0.0.2":{"remoteAs":65001,"state":"Established"}}}}`}
	applier := &bundle.Applier{Root: root, PubKey: pub, Runner: run, AllowUnbound: true}
	collector := &agentstatus.Collector{
		Runner: run, StateDir: state, Hashes: applier.Hashes, BundleID: applier.LastBundleID,
		Health: func() (bool, bool) { return true, false }, Now: time.Now,
	}
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	s := grpc.NewServer()
	pb.RegisterAgentServer(s, &Server{Applier: applier, Collector: collector, Interval: 20 * time.Millisecond, Heartbeat: time.Hour})
	go s.Serve(lis)
	t.Cleanup(s.Stop)
	conn, err := grpc.NewClient(lis.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return pb.NewAgentClient(conn), priv, root
}

func signed(priv ed25519.PrivateKey, id string, files map[string]string) *pb.ApplyBundleRequest {
	b := &pb.Bundle{Id: id}
	for p, c := range files {
		b.Files = append(b.Files, &pb.File{Path: p, Content: []byte(c)})
	}
	raw, _ := proto.MarshalOptions{Deterministic: true}.Marshal(b)
	return &pb.ApplyBundleRequest{Bundle: raw, Signature: ed25519.Sign(priv, raw)}
}

func TestApplyThenStatusOverGRPC(t *testing.T) {
	client, priv, root := start(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	resp, err := client.ApplyBundle(ctx, signed(priv, "rev-7", map[string]string{"frr/frr.conf": "x\n"}))
	if err != nil || !resp.Ok {
		t.Fatalf("apply: %v %+v", err, resp)
	}
	if data, _ := os.ReadFile(filepath.Join(root, "frr", "frr.conf")); string(data) != "x\n" {
		t.Fatal("file not written")
	}
	st, err := client.GetStatus(ctx, &pb.GetStatusRequest{})
	if err != nil {
		t.Fatal(err)
	}
	if st.BundleId != "rev-7" || st.FileHashes["frr/frr.conf"] != bundle.Hash([]byte("x\n")) {
		t.Fatalf("status: %+v", st)
	}
	if len(st.Bgp) != 1 || st.Bgp[0].State != "Established" || st.Bgp[0].RemoteAsn != 65001 {
		t.Fatalf("bgp: %+v", st.Bgp)
	}
	if len(st.Vrrp) != 1 || st.Vrrp[0].Name != "edge" || st.Vrrp[0].State != "MASTER" {
		t.Fatalf("vrrp: %+v", st.Vrrp)
	}
	if !st.HaproxyHealthy || st.RoutesWithdrawn {
		t.Fatalf("health: %+v", st)
	}
}

func TestWatchSendsAnUpdateWhenSomethingChanges(t *testing.T) {
	client, priv, _ := start(t)
	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	stream, err := client.Watch(ctx, &pb.WatchRequest{})
	if err != nil {
		t.Fatal(err)
	}
	first, err := stream.Recv()
	if err != nil || first.BundleId != "" {
		t.Fatalf("first: %v %+v", err, first)
	}
	if _, err := client.ApplyBundle(ctx, signed(priv, "rev-8", map[string]string{"frr/frr.conf": "y\n"})); err != nil {
		t.Fatal(err)
	}
	// Updates in between (files written, bundle id not yet recorded) are allowed; rev-8 must follow.
	for {
		next, err := stream.Recv()
		if err != nil {
			t.Fatalf("no update with bundle rev-8: %v", err)
		}
		if next.BundleId == "rev-8" {
			return
		}
	}
}

func TestRejectedBundleChangesNothingOverGRPC(t *testing.T) {
	client, _, root := start(t)
	_, other, _ := ed25519.GenerateKey(nil)
	resp, err := client.ApplyBundle(context.Background(), signed(other, "x", map[string]string{"frr/frr.conf": "evil"}))
	if err != nil || resp.Ok {
		t.Fatalf("expected an in-band rejection: %v %+v", err, resp)
	}
	if _, err := os.Stat(filepath.Join(root, "frr")); !os.IsNotExist(err) {
		t.Fatal("nothing may be written")
	}
}

func captureClient(t *testing.T, c *capture.Capturer) pb.AgentClient {
	t.Helper()
	lis, err := net.Listen("tcp", "127.0.0.1:0")
	if err != nil {
		t.Fatal(err)
	}
	s := grpc.NewServer()
	pb.RegisterAgentServer(s, &Server{Capturer: c})
	go s.Serve(lis)
	t.Cleanup(s.Stop)
	conn, err := grpc.NewClient(lis.Addr().String(), grpc.WithTransportCredentials(insecure.NewCredentials()))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { conn.Close() })
	return pb.NewAgentClient(conn)
}

func fakeCapturer(data string, stderr string) *capture.Capturer {
	return &capture.Capturer{
		Interfaces: func() ([]string, error) { return []string{"eth0"}, nil },
		Start: func(_ context.Context, _ []string) (io.ReadCloser, func() string, func() error, error) {
			return io.NopCloser(strings.NewReader(data)), func() string { return stderr }, func() error { return nil }, nil
		},
	}
}

func collect(t *testing.T, c pb.AgentClient, req *pb.CaptureRequest) ([]byte, *pb.CaptureChunk, error) {
	t.Helper()
	stream, err := c.Capture(context.Background(), req)
	if err != nil {
		return nil, nil, err
	}
	var data []byte
	var last *pb.CaptureChunk
	for {
		chunk, err := stream.Recv()
		if err == io.EOF {
			return data, last, nil
		}
		if err != nil {
			return data, last, err
		}
		data = append(data, chunk.Data...)
		last = chunk
	}
}

func TestCaptureStreamsThePcapAndTheTotals(t *testing.T) {
	c := captureClient(t, fakeCapturer("PCAP", "7 packets captured\n"))
	data, last, err := collect(t, c, &pb.CaptureRequest{Interface: "eth0", Protocol: "udp", Port: 53, DurationS: 5, MaxPackets: 10})
	if err != nil || string(data) != "PCAP" || last == nil || !last.Done || last.Packets != 7 || last.Error != "" {
		t.Fatalf("data=%q last=%+v err=%v", data, last, err)
	}
}

func TestCaptureReportsARefusedRequestInTheStream(t *testing.T) {
	c := captureClient(t, fakeCapturer("", ""))
	_, last, err := collect(t, c, &pb.CaptureRequest{Interface: "eth0;reboot", DurationS: 5, MaxPackets: 10})
	if err != nil || last == nil || !last.Done || !strings.Contains(last.Error, "not a valid interface name") {
		t.Fatalf("last=%+v err=%v", last, err)
	}
}

func TestCaptureIsUnimplementedWithoutACapturer(t *testing.T) {
	c := captureClient(t, nil)
	_, _, err := collect(t, c, &pb.CaptureRequest{Interface: "any", DurationS: 5, MaxPackets: 10})
	if status.Code(err) != codes.Unimplemented {
		t.Fatalf("err=%v", err)
	}
}
