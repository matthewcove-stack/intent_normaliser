#!/usr/bin/env bash
set -euo pipefail
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"
pick_python() {
  if command -v python3 >/dev/null 2>&1; then
    PYTHON_CMD=(python3)
  elif command -v python >/dev/null 2>&1; then
    PYTHON_CMD=(python)
  elif command -v python.exe >/dev/null 2>&1; then
    PYTHON_CMD=(python.exe)
  elif command -v py.exe >/dev/null 2>&1; then
    PYTHON_CMD=(py.exe -3)
  elif command -v py >/dev/null 2>&1; then
    PYTHON_CMD=(py -3)
  else
    echo "ERROR: no python interpreter found" >&2
    exit 1
  fi
}
pick_python
"${PYTHON_CMD[@]}" - <<'PY'
import re, sys
from pathlib import Path
import yaml
root=Path('.')
manifest_path=root/'edge'/'app.yml'
compose_edge_path=root/'compose.edge.yml'
errors=[]
if not manifest_path.exists(): errors.append('missing edge/app.yml')
if not compose_edge_path.exists(): errors.append('missing compose.edge.yml')
if errors:
  [print(f'ERROR: {e}', file=sys.stderr) for e in errors]
  sys.exit(1)
manifest=yaml.safe_load(manifest_path.read_text(encoding='utf-8')) or {}
entry=manifest.get('entry_service')
if not isinstance(entry,str) or not entry: errors.append('edge/app.yml missing valid entry_service')
compose_files=sorted([p for p in root.iterdir() if p.is_file() and re.match(r'^(compose|docker-compose).+\.(yml|yaml)$', p.name)])
primary=None
for n in ('compose.yml','compose.yaml','docker-compose.yml','docker-compose.yaml'):
  p=root/n
  if p.exists(): primary=p; break
if primary is None and compose_files: primary=compose_files[0]
if primary is None: errors.append('no base compose file found')
http_ports={80,443,3000,5173,5000,8000,8001,8080}

def load(path):
  try:
    data=yaml.safe_load(path.read_text(encoding='utf-8'))
    return data if isinstance(data,dict) else {}
  except Exception as exc:
    errors.append(f'{path.name}: failed to parse YAML: {exc}')
    return {}

def cport(port):
  if isinstance(port,int): return port
  if isinstance(port,str):
    m=re.search(r':(\d+)(?:/(tcp|udp))?$',port)
    if m: return int(m.group(1))
    if port.isdigit(): return int(port)
  if isinstance(port,dict):
    t=port.get('target')
    if isinstance(t,int): return t
    if isinstance(t,str) and t.isdigit(): return int(t)
  return None

if primary is not None and entry:
  s=(load(primary).get('services') or {})
  if entry not in s: errors.append(f"entry_service '{entry}' not found in {primary.name}")
edge_net=(load(compose_edge_path).get('networks') or {}).get('edge')
if not isinstance(edge_net,dict):
  errors.append('compose.edge.yml missing networks.edge')
else:
  if edge_net.get('external') is not True: errors.append('compose.edge.yml networks.edge.external must be true')
  if edge_net.get('name')!='edge': errors.append('compose.edge.yml networks.edge.name must be edge')
for cf in compose_files:
  data=load(cf)
  nets=data.get('networks') or {}
  if isinstance(nets,dict):
    for n,cfg in nets.items():
      if isinstance(cfg,dict) and cfg.get('external'):
        dec=cfg.get('name') or n
        if dec!='edge': errors.append(f"{cf.name}: external network '{dec}' is not edge")
  svcs=data.get('services') or {}
  for name,svc in svcs.items():
    labels=svc.get('labels') if isinstance(svc,dict) else None
    if isinstance(labels,list) and any('traefik.' in str(x) for x in labels): errors.append(f"{cf.name}: service '{name}' still defines Traefik labels")
    if isinstance(labels,dict) and any('traefik.' in str(k) for k in labels.keys()): errors.append(f"{cf.name}: service '{name}' still defines Traefik labels")
  if entry not in svcs: continue
  ports=(svcs[entry] or {}).get('ports')
  if not isinstance(ports,list): continue
  for p in ports:
    cp=cport(p)
    if cp not in http_ports: continue
    if isinstance(p,str):
      if ':' in p and not (p.startswith('127.0.0.1:') or p.startswith('localhost:')):
        errors.append(f"{cf.name}: entry_service '{entry}' exposes public HTTP port '{p}'")
    elif isinstance(p,dict):
      if p.get('published') is not None and p.get('host_ip') not in ('127.0.0.1','localhost'):
        errors.append(f"{cf.name}: entry_service '{entry}' exposes public HTTP port mapping {p}")
    elif isinstance(p,int):
      errors.append(f"{cf.name}: entry_service '{entry}' exposes public HTTP port '{p}'")
if errors:
  [print(f'ERROR: {e}', file=sys.stderr) for e in errors]
  sys.exit(1)
print('edge validation passed')
PY
