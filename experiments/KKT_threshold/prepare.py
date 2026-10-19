from pathlib import Path
import shutil,json,hashlib,re
root=Path(__file__).resolve().parent
src=Path(__file__).resolve().parents[2]/'chaincode'
snap=root/'source_snapshot'; snap.mkdir(exist_ok=True)
for n in ['kkt_onchain.go','utils.go','types.go','types_mask.go']:
 shutil.copy2(src/n,snap/n)
def block(text,token):
 p=text.index(token); a=text.index('{',p); d=1; i=a+1
 while d:
  d+=(text[i]=='{')-(text[i]=='}');i+=1
 return text[p:i]
u=(src/'utils.go').read_text()
s='package main\nimport("math";"errors")\nconst(MaskScale int64=1000000; EScale int64=1000; LambdaScale int64=10000; RhoScale int64=10000; MaxSqrtInt64 int64=3037000499; horizonHours=24;parkCount=3;heatParkCount=2;eBlockLen=96)\n'
for f in ['isqrt','divRound','addSquare','checkedAdd','checkedMul','absInt64']:s+=block(u,'func '+f+'(')+'\n'
for n,t in [('types.go','ADMMState'),('types_mask.go','MaskedSubproblem')]:s+=block((src/n).read_text(),'type '+t+' struct')+'\n'
(root/'helpers.go').write_text(s)
shutil.copy2(src/'kkt_onchain.go',root/'kkt_onchain.go')
(root/'manifest.json').write_text(json.dumps({str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in snap.iterdir()},indent=2))
