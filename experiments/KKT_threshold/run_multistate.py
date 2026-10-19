from pathlib import Path
import os
os.environ['OPENBLAS_NUM_THREADS']='1'
import numpy as np, scipy.sparse as sp, scipy.optimize as opt, osqp,json,subprocess,csv,hashlib
root=Path(__file__).resolve().parent;base=root.parents[2];out=root/'results_multistate';out.mkdir(exist_ok=True)
def save(p,o):p.write_text(json.dumps(o,ensure_ascii=False,indent=2))
def ints(x):return np.rint(x*1e6).astype(np.int64).tolist()
allrows=[]; refs=[]
states=json.loads((root/'offline_states.json').read_text())
for state_id,state in states.items():
 for park in range(3):
  source=base/'链下上传信任/M1a离线Mask验证/M6完整闭环'/f'mask_registration_park{park+1}.json'
  m=json.loads(source.read_text(encoding='utf-8-sig'));m['maskScale']=int(m['maskScale']);save(root/'source_snapshot'/source.name,m)
  E=np.array(m['eRowsScaled'])/1e6;H=np.array(m['hE0Scaled'])/1e6;G=np.array(m['gMaskedScaled'])/1e6;h=np.array(m['hVecScaled'])/1e6;c=np.array(m['c0Scaled'])/1e6+np.array(m['x0eScaled'])/1e6@E
  rho=state['rhoScaled']/1e4
  lam=np.concatenate([state['lambda'+e+'Scaled'][park] if e!='H' or park<2 else [0]*24 for e in 'EGHC'])/1e4
  z=np.concatenate([state['z'+e+'Scaled'][park] if e!='H' or park<2 else [0]*24 for e in 'EGHC'])/1e3
  H=rho*H;c=np.array(m['c0Scaled'])/1e6+rho*np.array(m['x0eScaled'])/1e6@E+(lam-rho*z)@E
  solver=osqp.OSQP();solver.setup(P=sp.csc_matrix(H),q=c,A=sp.csc_matrix(G),l=np.full(len(h),-np.inf),u=h,eps_abs=1e-9,eps_rel=1e-9,max_iter=20000,polishing=True,verbose=False);r=solver.solve();y=r.x;mu=r.y
  if r.info.status_val not in (1,2):raise RuntimeError(r.info.status)
  obj=lambda v:float(.5*v@H@v+c@v)
  rng=np.random.default_rng(42+park); direction=rng.normal(size=len(y));lp=opt.linprog(direction,A_ub=G,b_ub=h,bounds=[(None,None)]*len(y),method='highs')
  if not lp.success:raise RuntimeError(lp.message)
  cases=[]; details={}
  def add(name,v,w,kind,a):
   vy=np.array(ints(v))/1e6;wm=np.array(ints(w))/1e6
   cases.append(dict(Name=name,Y=ints(v),Mu=ints(w)))
   details[name]=dict(state_iteration=int(state_id),rho=rho,park=park+1,kind=kind,alpha=a,float_stationarity=float(np.linalg.norm(H@vy+c+G.T@wm)),float_primal_violation=float(max(0,np.max(G@vy-h))),float_dual_violation=float(max(0,-wm.min())),float_complementarity=float(np.max(np.abs(wm*(G@vy-h)))),objective=obj(vy),objective_difference_from_numerical_reference=obj(vy)-obj(y),distance_from_reference=float(np.linalg.norm(vy-y)))
  add('normal',y,mu,'normal_numerical_solution',0)
  for a in np.logspace(-8,-1,29):add('feasible_'+str(a),(1-a)*y+a*lp.x,mu,'feasible_convex_interpolation',float(a))
  for a in np.logspace(-6,-1,21):
   w=mu.copy();w[np.argmin(mu)]=-a
   add('dual_'+str(a),y,w,'negative_multiplier_only',float(a))
  for a in np.logspace(-7,-2,21):add('primal_'+str(a),y+a*direction,mu,'primal_direction_perturbation',float(a))
  inp=dict(Mask=m,State=state,Park=park,Cases=cases);save(out/f'input_state{state_id}_park{park+1}.json',inp)
  proc=subprocess.run([str(root/'kkt_check')],input=json.dumps(inp),text=True,capture_output=True,check=True);raw=json.loads(proc.stdout);save(out/f'go_residuals_state{state_id}_park{park+1}.json',raw)
  for row in raw:row.update(details[row['name']]);allrows.append(row)
  refs.append(dict(state_iteration=int(state_id),rho=rho,park=park+1,status=r.info.status,iterations=r.info.iter,objective=obj(y),stationarity=float(np.linalg.norm(H@y+c+G.T@mu)),primal_violation=float(max(0,np.max(G@y-h))),dual_violation=float(max(0,-mu.min())),complementarity=float(np.max(np.abs(mu*(G@y-h)))),hessian_min_eigenvalue=float(np.linalg.eigvalsh(H)[0]),HE0_vs_EtE_spectral_difference=float(np.linalg.norm(H-rho*E.T@E,2)),lp_status=lp.message))
  print('completed state',state_id,'park',park+1,flush=True)
save(out/'references.json',refs)
with (out/'all_candidates.csv').open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=list(allrows[0]));w.writeheader();w.writerows(allrows)
summary=[]
for tol in [1000,2000,5000,10000,20000,50000]:
 for kind in sorted(set(r['kind'] for r in allrows)):
  rr=[r for r in allrows if r['kind']==kind];passed=[r for r in rr if not r['saturated'] and max(r[k] for k in ['stationarity','complementarity','primal_violation','dual_violation'])<=tol]
  summary.append(dict(threshold=tol,kind=kind,total=len(rr),accepted=len(passed),max_objective_difference_accepted=max([r['objective_difference_from_numerical_reference'] for r in passed],default=None),max_float_primal_violation_accepted=max([r['float_primal_violation'] for r in passed],default=None)))
save(out/'threshold_summary.json',summary)
manifest={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'source_snapshot').iterdir()};save(root/'manifest.json',manifest)
