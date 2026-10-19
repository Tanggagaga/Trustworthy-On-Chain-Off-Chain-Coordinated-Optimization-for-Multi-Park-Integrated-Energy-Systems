% Audit against the original ADMM constraint block (extracted unchanged).
clear;
root=fileparts(mfilename('fullpath')); addpath(root);
S=load(fullfile(root,'admm4e_results.mat'));
R=load(fullfile(root,'centralized_matched_4e_results.mat'));
for group={'data','p','cap'}
    g=group{1}; names=fieldnames(R.(g));
    for j=1:numel(names)
        assert(isequal(R.(g).(names{j}),S.(g).(names{j})),['Input mismatch: ',g,'.',names{j}]);
    end
end
fprintf('Stored input data and parameters exactly match centralized inputs.\n');
C=[]; F=0; v=cell(3,1); local=cell(3,1);
for m=1:3
    [local{m},Fm,v{m}]=original_local_model(m,S.data,S.p,S.cap);
    C=[C,local{m}]; F=F+Fm;
end
coupling=[v{1}.eE+v{2}.eE+v{3}.eE==0,v{1}.eG+v{2}.eG+v{3}.eG==0, ...
    v{1}.eC+v{2}.eC+v{3}.eC==0,v{1}.eH+v{2}.eH==0];
opts=sdpsettings('solver','gurobi','verbose',0);
diagnostic=optimize([C,coupling],F,opts);
assert(diagnostic.problem==0,diagnostic.info);
originalBlockOptimum=value(F);
assert(abs(originalBlockOptimum-R.F_value)<1e-6,'Centralized model differs from original blocks');
centralNumeric=R.centralSolutions;
for m=1:3, put_values(v{m},centralNumeric{m},S.p,m); end
centralViolation=max(0,-min(check([C,coupling])));
assert(centralViolation<1e-7);
for m=1:3, put_values(v{m},S.last_sol{m},S.p,m); end
recomputedStoredCost=value(F);
storedCost=S.hist(end,4);
assert(abs(recomputedStoredCost-storedCost)<1e-6,'Stored cost cannot be reconstructed');
localViolation=max(0,-min(check(C)));
globalViolation=max(0,-min(check(coupling)));
allE=[S.eE;S.eG;S.eH;S.eC]; allZ=[S.z_E;S.z_G;S.z_H;S.z_C];
recomputedPrimal=norm(allE-allZ,'fro');
assert(abs(recomputedPrimal-S.hist(end,2))<1e-9);
fprintf('Original-block optimum %.12f; central violation %.3g\n',originalBlockOptimum,centralViolation);
fprintf('Stored cost recomputed %.12f; local violation %.3g; global violation %.9f MW\n',recomputedStoredCost,localViolation,globalViolation);

% A separate feasible recovery diagnostic: keep the saved coordinated z
% fixed, and redispatch only local equipment. Never relabel it as raw ADMM.
fixZ=[]; labels={'eE','eG','eH','eC'}; zs={S.z_E,S.z_G,S.z_H,S.z_C};
for m=1:3
    for j=1:4, fixZ=[fixZ,v{m}.(labels{j})==zs{j}(m,:)]; end
end
recovery=optimize([C,coupling,fixZ],F,opts);
recoveredCost=NaN; recoveredViolation=NaN;
if recovery.problem==0
    recoveredCost=value(F);
    recoveredViolation=max(0,-min(check([C,coupling,fixZ])));
end
fprintf('Fixed-z recovery status %d; cost %.12f; violation %.3g\n',recovery.problem,recoveredCost,recoveredViolation);
% Diagnose infeasibility explicitly (disable presolve dual reductions).
opts.gurobi.DualReductions=0;
fixedDiagnosis=optimize([C,coupling,fixZ],F,opts);
zBoundViolation=0;
bounds={S.cap.eE_max,S.cap.eG_max,S.cap.eH_max,S.cap.eC_max};
for j=1:4
    zBoundViolation=max(zBoundViolation,max(max(abs(zs{j})-bounds{j}(:))));
end
% Supplemental recovery with the smallest total absolute change to saved z.
% This is an additional centralized postprocessing experiment, NOT ADMM.
deviation=0;
for m=1:3
    for j=1:4, deviation=deviation+sum(abs(v{m}.(labels{j})-zs{j}(m,:))); end
end
repair1=optimize([C,coupling],deviation,opts);
assert(repair1.problem==0,repair1.info);
minExchangeChange=value(deviation);
repairConstraints=[C,coupling,deviation<=minExchangeChange+1e-7];
repair2=optimize(repairConstraints,F,opts);
assert(repair2.problem==0,repair2.info);
repairCost=value(F); repairViolation=max(0,-min(check([C,coupling])));
repairSolutions=cell(3,1);
for m=1:3
    names=fieldnames(v{m});
    for j=1:numel(names), repairSolutions{m}.(names{j})=value(v{m}.(names{j})); end
end
fprintf('Fixed-z diagnostic status %d; z box violation %.9f MW\n',fixedDiagnosis.problem,zBoundViolation);
fprintf('Minimum L1 exchange adjustment %.9f; repaired cost %.12f; gap %.9f%%\n',minExchangeChange,repairCost,100*(repairCost-originalBlockOptimum)/originalBlockOptimum);

A=struct('inputs_match',true,'original_block_optimum',originalBlockOptimum, ...
    'central_max_violation',centralViolation,'stored_cost_recomputed',recomputedStoredCost, ...
    'stored_local_max_violation',localViolation,'stored_global_max_violation_MW',globalViolation, ...
    'stored_primal_residual',recomputedPrimal,'stored_iterations',size(S.hist,1), ...
    'fixed_z_recovery_status',recovery.problem,'fixed_z_recovery_info',recovery.info, ...
    'fixed_z_recovery_cost',recoveredCost,'fixed_z_recovery_max_violation',recoveredViolation, ...
    'fixed_z_diagnosis_status',fixedDiagnosis.problem,'fixed_z_diagnosis_info',fixedDiagnosis.info, ...
    'z_bound_violation_MW',zBoundViolation,'minimum_total_exchange_adjustment',minExchangeChange, ...
    'extra_repair_cost',repairCost,'extra_repair_max_violation',repairViolation);
replay=load(fullfile(root,'replay47','admm4e_results.mat'));
A.replay_iterations=size(replay.hist,1);
A.replay_cost=replay.hist(end,4);
A.replay_history_max_difference=max(abs(replay.hist(:,[1:4,6])-S.hist(:,[1:4,6])),[],'all');
save(fullfile(root,'audit_results.mat'),'A','recovery','repairSolutions');
fid=fopen(fullfile(root,'audit_results.json'),'w','n','UTF-8'); fprintf(fid,'%s',jsonencode(A,PrettyPrint=true)); fclose(fid);
Method=["Centralized (same original blocks)";"Stored ADMM at iteration 47";"Fixed-z recovery (infeasible)";"Minimum exchange adjustment recovery (additional centralized postprocessing)"];
Cost=[originalBlockOptimum;recomputedStoredCost;recoveredCost;repairCost];
SignedDifference=Cost-originalBlockOptimum;
RelativeDifferencePct=100*SignedDifference/abs(originalBlockOptimum);
MaxConstraintViolation=[centralViolation;max(localViolation,globalViolation);recoveredViolation;repairViolation];
writetable(table(Method,Cost,SignedDifference,RelativeDifferencePct,MaxConstraintViolation),fullfile(root,'audited_comparison.csv'));

function put_values(v,s,p,m)
names=fieldnames(v);
for j=1:numel(names)
    name=names{j};
    if isfield(s,name), assign(v.(name),s.(name)); end
end
% Historical MAT contains the excess and carbon cost, but not the segments.
% Rebuild the cheapest feasible four-tier allocation from actual emissions.
ex=max(0,p.ef_gas*sum(s.Gbuy)+p.ef_grid*sum(s.Pbuy)-p.E_quota(m));
seg=zeros(1,4); remaining=ex;
for j=1:3, seg(j)=min(p.carbon_step,remaining); remaining=remaining-seg(j); end
seg(4)=remaining;
assign(v.carbon_excess,ex); assign(v.carbon_seg,seg);
end
