# 0065. BGP prefix limits

Status: Accepted
Date: 2026-10-11

## Context

The routing profile in 01 lists "BGP ASN, peers, BFD, prefix limits". The model and the FRR template had no limit.

## Decisions

1. `BgpProfile.max_prefixes` (default for all neighbours) and `BgpPeer.max_prefixes` (override), both optional and between 1 and
   10 000 000. With none set nothing is rendered, so existing groups are unchanged.
2. The renderer writes `neighbor <ip> maximum-prefix <n> restart 5` in the neighbour's address family. FRR resets the session when
   the neighbour announces more prefixes than the limit and tries again after five minutes, so a one-off flood heals itself.
3. The SE accepts nothing from its neighbours (`route-map SY-IN deny`), so the limit is a guard against a misconfigured or
   hostile upstream, not a table size. Choose a small number.
4. A tripped limit drops the session and with it the VIP announcements to that neighbour until FRR reconnects. That is the
   intent; ECMP over the other neighbours keeps serving.
5. The profile is stored as JSON (`routing_profile.spec`), so no migration is needed. Golden case `06-bgp-prefix-limits`, validated by FRR itself in
   the routing-validate test.
