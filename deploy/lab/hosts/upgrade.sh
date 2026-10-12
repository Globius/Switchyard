#!/bin/sh
# Rolling upgrade of an SE group to a new template, or scale-out when run with --add. One engine at a time, health-gated:
# the group never drops below what it had. Works through the controller API and the lab helpers (lib.sh, hosts.env).
#   sh upgrade.sh GROUP KIND "NODE=TEMPLATE_ID NODE=TEMPLATE_ID ..."   replace every engine with one cloned from the new template
#   sh upgrade.sh --add GROUP KIND "NODE=TEMPLATE_ID" [N]               add N engines (default 1) on those nodes
# Per engine: deploy a replacement on the same node, enroll it, push the group's current configuration to it (reapply),
# wait until the group scores 80 or more, retire the old engine, wait for the score again. Stops at the first failure and leaves both engines in place, so
# the old one keeps serving. KIND is lxc or qemu. LAB_TENANT selects the tenant (default lab).
set -eu
cd "$(dirname "$0")"
[ -f hosts.env ] && . ./hosts.env
. ./lib.sh
ADD=""; [ "${1:-}" = --add ] && { ADD=1; shift; }
GROUP=${1:?group}; KIND=${2:?lxc or qemu}; MAP=${3:?"NODE=TEMPLATE ..."}; COUNT=${4:-1}
tmpl_for() { for p in $MAP; do [ "${p%%=*}" = "$1" ] && echo "${p#*=}"; done; }
score_ok() { api health | getj "g=next(x for x in d if x['name']=='$GROUP');print(1 if g['score']>=80 and all(e['score']>=80 for e in g['engines']) else 0)" | tr -d '\r'; }
is_reachable() { api status | getj "print(any(e['engine']=='$1' and e['reachable'] for e in d))" | tr -d "\015" | grep -q True; }
engines_of() { api engines "$GROUP" | tr -d '\r'; }
# Deploy one more engine on NODE from its template, enroll it, wait for it. Prints the new engine's name.
grow() { node=$1
  n=$(engines_of | getj "print(len(d))" | tr -d '\r')
  api deploy "$GROUP" $((n+1)) "$node" "$(tmpl_for "$node")" "$KIND" > work/up-deploy.json
  new=$(getj "print(next(e['name'] for e in d['engines'] if 'token' in e))" < work/up-deploy.json | tr -d '\r')
  [ "$KIND" = lxc ] && lxc_mtu_all work/up-deploy.json
  enroll_all work/up-deploy.json "$KIND"
  wait_for "$new reachable" "is_reachable $new" 40
  api repair "$GROUP" >/dev/null   # the new engine gets the group's current configuration (a canary-first apply)
  wait_for "$new and the group healthy" "[ \"\$(score_ok)\" = 1 ]" 60
  echo "$new"; }
if [ -n "$ADD" ]; then
  i=0; while [ $i -lt "$COUNT" ]; do for p in $MAP; do step "scale out: +1 engine on ${p%%=*}"; grow "${p%%=*}" >/dev/null; done; i=$((i+1)); done
  api health | getj "g=next(x for x in d if x['name']=='$GROUP');print('group', g['name'], 'score', g['score'], 'engines', len(g['engines']))"
  exit 0
fi
engines_of > work/up-old.json
python -c 'import json
for e in json.load(open("work/up-old.json")): print(e["name"], e["pve_node"])' | tr -d '\r' > work/up-old.list
wait_for "group healthy before the upgrade" "[ \"\$(score_ok)\" = 1 ]" 20
exec 5< work/up-old.list
while read -r old node <&5; do
  step "replace $old on $node"
  new=$(grow "$node" | tail -1)
  api retire "$old" >/dev/null
  wait_for "group healthy after retiring $old" "[ \"\$(score_ok)\" = 1 ]" 60
  echo "   $old -> $new"
done
exec 5<&-
api health | getj "g=next(x for x in d if x['name']=='$GROUP');print('group', g['name'], 'score', g['score'], [e['engine'] for e in g['engines']])"
