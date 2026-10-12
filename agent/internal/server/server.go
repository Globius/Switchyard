// SPDX-FileCopyrightText: 2026 The Switchyard contributors
//
// SPDX-License-Identifier: Apache-2.0

// Package server implements the Agent gRPC service.
package server

import (
	"context"
	"path/filepath"
	"time"

	"google.golang.org/protobuf/proto"

	pb "switchyard/agent/gen/switchyard/agent/v1"
	"switchyard/agent/internal/bundle"
	"switchyard/agent/internal/probe"
	"switchyard/agent/internal/status"
)

// Server implements pb.AgentServer.
type Server struct {
	pb.UnimplementedAgentServer
	Applier   *bundle.Applier
	Collector *status.Collector
	// Root is the managed directory (/etc/switchyard); probes may only target its configured members.
	Root string
	// Interval is how often Watch re-checks; Heartbeat forces a send even without changes.
	Interval  time.Duration
	Heartbeat time.Duration
}

// ApplyBundle verifies and applies a bundle.
func (s *Server) ApplyBundle(ctx context.Context, req *pb.ApplyBundleRequest) (*pb.ApplyBundleResponse, error) {
	return s.Applier.Apply(ctx, req), nil
}

// GetStatus returns a status snapshot.
func (s *Server) GetStatus(ctx context.Context, _ *pb.GetStatusRequest) (*pb.Status, error) {
	return s.Collector.Collect(ctx), nil
}

// ProbeMember runs one health probe against a member of the applied configuration.
func (s *Server) ProbeMember(ctx context.Context, req *pb.ProbeRequest) (*pb.ProbeResponse, error) {
	allowed := probe.Members(
		filepath.Join(s.Root, "haproxy", "haproxy.cfg"),
		filepath.Join(s.Root, "keepalived", "keepalived.conf"),
	)
	res := probe.Run(ctx, probe.Request{
		Protocol:     req.Protocol,
		Address:      req.Address,
		Port:         req.Port,
		Timeout:      time.Duration(req.TimeoutMs) * time.Millisecond,
		HTTPMethod:   req.HttpMethod,
		HTTPPath:     req.HttpPath,
		HTTPHost:     req.HttpHost,
		ExpectStatus: req.ExpectStatus,
		UDPPayload:   req.UdpPayload,
		UDPExpect:    req.UdpExpect,
	}, allowed)
	return &pb.ProbeResponse{
		Ok:        res.OK,
		LatencyMs: uint32(res.Latency / time.Millisecond),
		Detail:    res.Detail,
		Refused:   res.Refused,
	}, nil
}

func comparable(st *pb.Status) *pb.Status {
	c := proto.Clone(st).(*pb.Status)
	c.ObservedAtUnix = 0
	return c
}

// Watch streams a snapshot when something changes, and at least every Heartbeat.
func (s *Server) Watch(_ *pb.WatchRequest, stream pb.Agent_WatchServer) error {
	interval, heartbeat := s.Interval, s.Heartbeat
	if interval == 0 {
		interval = 5 * time.Second
	}
	if heartbeat == 0 {
		heartbeat = 30 * time.Second
	}
	ctx := stream.Context()
	var last *pb.Status
	lastSent := time.Time{}
	ticker := time.NewTicker(interval)
	defer ticker.Stop()
	for {
		st := s.Collector.Collect(ctx)
		if last == nil || !proto.Equal(comparable(st), comparable(last)) || time.Since(lastSent) >= heartbeat {
			if err := stream.Send(st); err != nil {
				return err
			}
			last, lastSent = st, time.Now()
		}
		select {
		case <-ctx.Done():
			return nil
		case <-ticker.C:
		}
	}
}
