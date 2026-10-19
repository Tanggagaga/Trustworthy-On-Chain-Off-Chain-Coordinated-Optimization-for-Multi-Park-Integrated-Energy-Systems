% Strict centralized reference for the reported four-energy ADMM model.
% This file intentionally uses the same data, parameters, local constraints,
% exchange bounds, and unaugmented objective as admm_100iters.m.
% It does not use bilateral line losses, the older 集中式cc model, or an
% exchange regularizer.

clear;
thisDir = fileparts(mfilename('fullpath'));
T = 24; M = 3;

parkFiles = {
    'park1_commercial_integrated_timeseries.csv'
    'park2_industrial_waste_heat_timeseries.csv'
    'park3_wind_p2g_export_timeseries.csv'};

data.load_e = zeros(M,T); data.load_g = zeros(M,T);
data.load_h = zeros(M,T); data.load_c = zeros(M,T);
data.P_PV = zeros(M,T); data.P_WT = zeros(M,T);
data.waste_heat_avail = zeros(1,T);
for m = 1:M
    tbl = readtable(fullfile(thisDir, parkFiles{m}));
    data.load_e(m,:) = tbl.load_e'; data.load_g(m,:) = tbl.load_g';
    data.load_h(m,:) = tbl.load_h'; data.load_c(m,:) = tbl.load_c';
    data.P_PV(m,:) = tbl.P_PV'; data.P_WT(m,:) = tbl.P_WT';
    if m == 2
        data.waste_heat_avail = tbl.waste_heat_available';
    end
end

[p, cap] = get_parameters_same_model();
opts = sdpsettings('solver','gurobi','verbose',1);

% Build one centralized model from the exact local feasible sets.
C = []; F = 0;
E = cell(M,1); G = cell(M,1); H = cell(M,1); W = cell(M,1);
localCost = cell(M,1); vars = cell(M,1);
for m = 1:M
    [Cm, Fm, vm] = build_local_model_same_model(m, data, p, cap, T);
    C = [C, Cm]; F = F + Fm;
    E{m} = vm.eE; G{m} = vm.eG; H{m} = vm.eH; W{m} = vm.eC;
    localCost{m} = Fm; vars{m} = vm;
end

% Exact global coupling used by the ADMM coordinator.
for t = 1:T
    C = [C, E{1}(t)+E{2}(t)+E{3}(t) == 0];
    C = [C, G{1}(t)+G{2}(t)+G{3}(t) == 0];
    C = [C, W{1}(t) + W{2}(t) + W{3}(t) == 0];
    C = [C, H{1}(t) + H{2}(t) == 0, H{3}(t) == 0];
end

tic;
result = optimize(C, F, opts);
centralWallSeconds=toc;
if result.problem ~= 0
    error('Matched centralized solve failed: %s', result.info);
end

F_value = value(F);
centralSolutions=cell(M,1);
for m=1:M
    names=fieldnames(vars{m});
    for j=1:numel(names)
        centralSolutions{m}.(names{j})=value(vars{m}.(names{j}));
    end
end
centralConstraintViolation=max(0,-min(check(C)));
fprintf('\nMatched centralized objective: %.12f\n', F_value);
fprintf('Solver: %s\n', result.info);

% Extract and check balances/global coupling.
summary = table();
maxBalance = zeros(M,4);
parkObj = zeros(M,1);
for m = 1:M
    vm = vars{m};
    parkObj(m) = value(localCost{m});
    maxBalance(m,:) = [max(abs(value(vm.elecBalance))), ...
        max(abs(value(vm.gasBalance))), max(abs(value(vm.heatBalance))), ...
        max(abs(value(vm.coldBalance)))];
end
maxGlobal = [max(abs(sum(cell2mat(cellfun(@(x)value(x),E,'UniformOutput',false))))), ...
    max(abs(sum(cell2mat(cellfun(@(x)value(x),G,'UniformOutput',false))))), ...
    max(abs(value(W{1}+W{2}+W{3}))), max(abs(value(H{1}+H{2})))];

summary.park = (1:M)'; summary.local_objective = parkObj;
summary.max_electric_balance = maxBalance(:,1);
summary.max_gas_balance = maxBalance(:,2);
summary.max_heat_balance = maxBalance(:,3);
summary.max_cooling_balance = maxBalance(:,4);
writetable(summary, fullfile(thisDir,'centralized_matched_4e_park_summary.csv'));

metrics = table(F_value, maxGlobal(1), maxGlobal(2), maxGlobal(3), maxGlobal(4), ...
    max(maxBalance(:,1)), max(maxBalance(:,2)), max(maxBalance(:,3)), max(maxBalance(:,4)), ...
    'VariableNames', {'centralized_objective','global_E_residual','global_G_residual', ...
    'global_C_residual','global_H_residual','max_local_E_residual','max_local_G_residual', ...
    'max_local_H_residual','max_local_C_residual'});
writetable(metrics, fullfile(thisDir,'centralized_matched_4e_metrics.csv'));
save(fullfile(thisDir,'centralized_matched_4e_results.mat'), ...
    'F_value','parkObj','maxBalance','maxGlobal','result','data','p','cap', ...
    'centralSolutions','centralConstraintViolation','centralWallSeconds');

% Compare with the stored 47-iteration ADMM objective only after evaluating
% the same unaugmented local objective. The comparison is diagnostic; the
% stored ADMM trajectory remains the source for the paper's 47-iteration run.
admmSummary = readtable(fullfile(thisDir,'admm4e_summary.csv'));
admmObj = admmSummary.admm_obj(1);
absGap = admmObj - F_value;
relGapPct = 100*absGap/abs(F_value);
comparison = table(F_value, admmObj, absGap, relGapPct, admmSummary.iters(1), ...
    admmSummary.r_pri(1), admmSummary.r_dual(1), ...
    'VariableNames', {'matched_centralized_objective','admm_objective_iter47', ...
    'absolute_difference','relative_difference_pct','admm_iterations','admm_primal_residual','admm_dual_residual'});
writetable(comparison, fullfile(thisDir,'centralized_vs_admm_comparison.csv'));
fprintf('Stored ADMM objective: %.12f\n', admmObj);
fprintf('Absolute difference: %.12f\n', absGap);
fprintf('Relative difference: %.9f %%\n', relGapPct);

function [p, cap] = get_parameters_same_model()
p.e_G3=0.26; p.h_G3=0.68; p.EH=0.60; p.GH=0.85;
p.COP_AC=0.80; p.COP_EC=3.00; p.EG=0.60;
p.c_gas=0.25; p.c_buy=0.70; p.c_sell=0.20; p.c_waste_heat=0.02;
p.ef_gas=0.4483; p.ef_grid=0.8050; p.carbon_base_price=0.45;
p.carbon_growth=0.50; p.carbon_step=8; p.E_quota=[430,530,240];
cap.P_G3_min=[0,0,0]; cap.P_G3_max=[18,25,5.4];
cap.P_EH_max=[43,70,12]; cap.P_GH_max=[36,52,10.8];
cap.P_AC_max=[20,28,8]; cap.P_EC_max=[30,25,45]; cap.P_EG_max=[30,10,75];
cap.Pbuy_max=[15,24,8]; cap.Psell_min=[-15,-8,-5];
cap.eE_max=[11,14,9]; cap.eG_max=[7,13,14]; cap.eH_max=[9,9,0]; cap.eC_max=[2,4,2];
end

function [Con, F_local, v] = build_local_model_same_model(m,data,p,cap,T)
PG3=sdpvar(1,T,'full'); PEH=sdpvar(1,T,'full'); PGH=sdpvar(1,T,'full');
PAC=sdpvar(1,T,'full'); PEC=sdpvar(1,T,'full'); PEG=sdpvar(1,T,'full');
Pbuy=sdpvar(1,T,'full'); Psell=sdpvar(1,T,'full'); Gbuy=sdpvar(1,T,'full');
Hwaste=sdpvar(1,T,'full'); eE=sdpvar(1,T,'full'); eG=sdpvar(1,T,'full');
eH=sdpvar(1,T,'full'); eC=sdpvar(1,T,'full');
carbon_excess=sdpvar(1,1); carbon_seg=sdpvar(1,4);
Con=[]; elecBalance=sdpvar(1,T); gasBalance=sdpvar(1,T);
heatBalance=sdpvar(1,T); coldBalance=sdpvar(1,T);
for t=1:T
    Con=[Con,cap.P_G3_min(m)<=PG3(t)<=cap.P_G3_max(m),0<=PEH(t)<=cap.P_EH_max(m), ...
        0<=PGH(t)<=cap.P_GH_max(m),0<=PAC(t)<=cap.P_AC_max(m), ...
        0<=PEC(t)<=cap.P_EC_max(m),0<=PEG(t)<=cap.P_EG_max(m), ...
        0<=Pbuy(t)<=cap.Pbuy_max(m),cap.Psell_min(m)<=Psell(t)<=0,0<=Gbuy(t)];
    Con=[Con,PEH(t)<=PG3(t)/p.e_G3*p.h_G3*p.EH];
    Con=[Con,-cap.eE_max(m)<=eE(t)<=cap.eE_max(m),-cap.eG_max(m)<=eG(t)<=cap.eG_max(m), ...
        -cap.eH_max(m)<=eH(t)<=cap.eH_max(m),-cap.eC_max(m)<=eC(t)<=cap.eC_max(m)];
    if m==2, Con=[Con,0<=Hwaste(t)<=data.waste_heat_avail(t)]; else, Con=[Con,Hwaste(t)==0]; end
    if m==3, Con=[Con,eH(t)==0,eG(t)>=-PEG(t)]; end
    elecBalance(t)=data.P_WT(m,t)+data.P_PV(m,t)+Pbuy(t)+Psell(t)+PG3(t)+eE(t)-PEC(t)/p.COP_EC-PEG(t)/p.EG-data.load_e(m,t);
    gasBalance(t)=Gbuy(t)+PEG(t)+eG(t)-PG3(t)/p.e_G3-PGH(t)/p.GH-data.load_g(m,t);
    heatBalance(t)=PEH(t)+PGH(t)+Hwaste(t)+eH(t)-data.load_h(m,t)-PAC(t)/p.COP_AC;
    coldBalance(t)=PEC(t)+PAC(t)+eC(t)-data.load_c(m,t);
    Con=[Con,elecBalance(t)==0,gasBalance(t)==0,heatBalance(t)==0,coldBalance(t)==0];
end
emission=p.ef_gas*sum(Gbuy)+p.ef_grid*sum(Pbuy);
cp=p.carbon_base_price*[1,1+p.carbon_growth,1+2*p.carbon_growth,1+3*p.carbon_growth];
Con=[Con,carbon_excess>=0,carbon_excess>=emission-p.E_quota(m),carbon_excess==sum(carbon_seg),carbon_seg>=0];
Con=[Con,carbon_seg(1)<=p.carbon_step,carbon_seg(2)<=p.carbon_step,carbon_seg(3)<=p.carbon_step];
F_local=p.c_gas*sum(Gbuy)+p.c_buy*sum(Pbuy)+p.c_sell*sum(Psell)+cp*carbon_seg'+p.c_waste_heat*sum(Hwaste);
v=struct('eE',eE,'eG',eG,'eH',eH,'eC',eC,'elecBalance',elecBalance,'gasBalance',gasBalance,'heatBalance',heatBalance,'coldBalance',coldBalance, ...
    'PG3',PG3,'PEH',PEH,'PGH',PGH,'PAC',PAC,'PEC',PEC,'PEG',PEG, ...
    'Pbuy',Pbuy,'Psell',Psell,'Gbuy',Gbuy,'Hwaste',Hwaste,'carbon_excess',carbon_excess,'carbon_seg',carbon_seg);
end
