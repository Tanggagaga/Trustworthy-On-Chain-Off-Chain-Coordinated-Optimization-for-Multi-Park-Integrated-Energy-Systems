function result=run_one(cfg)
%% ── Paths ───────────────────────────────────────────────────────────────────
thisDir   = fileparts(mfilename('fullpath'));
designDir = fullfile(thisDir,'source');

parkFiles = {
    'park1_commercial_integrated_timeseries.csv'
    'park2_industrial_waste_heat_timeseries.csv'
    'park3_wind_p2g_export_timeseries.csv'
};
parkNames = {'park1_commercial','park2_industrial','park3_wind_p2g'};
M = 3;  T = 24;

%% ── Load time-series data ───────────────────────────────────────────────────
data.load_e          = zeros(M,T);  data.load_g = zeros(M,T);
data.load_h          = zeros(M,T);  data.load_c = zeros(M,T);
data.P_PV            = zeros(M,T);  data.P_WT   = zeros(M,T);
data.waste_heat_avail= zeros(1,T);  % Park2 only

for m = 1:M
    tbl = readtable(fullfile(designDir, parkFiles{m}));
    data.load_e(m,:) = tbl.load_e';  data.load_g(m,:) = tbl.load_g';
    data.load_h(m,:) = tbl.load_h';  data.load_c(m,:) = tbl.load_c';
    data.P_PV(m,:)   = tbl.P_PV';   data.P_WT(m,:)   = tbl.P_WT';
    if m == 2
        data.waste_heat_avail = tbl.waste_heat_available';
    end
end
fprintf('Data loaded: %d parks × %d hours.\n', M, T);

%% ── Parameters & device capacities ──────────────────────────────────────────
[p, cap] = get_parameters();

F_central=NaN;
%% ── ADMM initialisation ──────────────────────────────────────────────────────
% Multipliers: [M×T] each
lambda_E = zeros(M,T);  lambda_G = zeros(M,T);
lambda_H = zeros(M,T);  lambda_C = zeros(M,T);
% lambda_H(3,:) stays 0 throughout (Park3 not in heat network)

% Coordinator allocations (all satisfy sum=0 or heat 2-park sum=0)
z_E = zeros(M,T);  z_G = zeros(M,T);
z_H = zeros(M,T);  z_C = zeros(M,T);

% ADMM hyper-parameters
rho = cfg.rho0;
mu = cfg.mu;
omega = cfg.tau;
rho_min = 0.05;
rho_max = 100.0;

maxIter  = 100;
eps_pri = cfg.eps;   % MW  (combined over all 4 energies)
eps_dual = cfg.eps;

hist = nan(maxIter, 6);
last_sol = cell(M,1);
opts = sdpsettings('solver','gurobi','verbose',0);

fprintf('=== ADMM 4-Energy iterations (rho=%.2f) ===\n', rho);
fprintf('%-6s %10s %10s %12s %10s %8s\n', ...
    'iter','r_pri','r_dual','local_obj','gap%','rho');

%% ── ADMM main loop ───────────────────────────────────────────────────────────
extra=zeros(maxIter,4); timer=tic;
for k = 1:maxIter
 rho_used=rho;

    z_E_old = z_E;  z_G_old = z_G;
    z_H_old = z_H;  z_C_old = z_C;

    %% 4a. Park subproblems (independent, given coordinator allocations & multipliers)
    for m = 1:M
        last_sol{m} = solve_park(m, data, p, cap, ...
            lambda_E(m,:), lambda_G(m,:), lambda_H(m,:), lambda_C(m,:), ...
            z_E(m,:), z_G(m,:), z_H(m,:), z_C(m,:), rho, opts);
        if last_sol{m}.problem ~= 0
            fprintf('Park %d FAILED at iter %d: %s\n', m, k, last_sol{m}.info);
            error('ADMM aborted.');
        end
    end

    % Collect each park's net exchange
    eE = zeros(M,T); eG = zeros(M,T); eH = zeros(M,T); eC = zeros(M,T);
    for m = 1:M
        eE(m,:) = last_sol{m}.eE;  eG(m,:) = last_sol{m}.eG;
        eH(m,:) = last_sol{m}.eH;  eC(m,:) = last_sol{m}.eC;
    end

    %% 4b. Coordinator update (closed-form)
    % Electric, Gas, Cold — all 3 parks, project onto sum=0 hyperplane
    vE = eE + lambda_E/rho;  z_E = vE - ones(M,1)*mean(vE,1);
    vG = eG + lambda_G/rho;  z_G = vG - ones(M,1)*mean(vG,1);
    vC = eC + lambda_C/rho;  z_C = vC - ones(M,1)*mean(vC,1);

    % Heat — only Park1 & Park2, project onto z_H1+z_H2=0, z_H3=0
    vH1 = eH(1,:) + lambda_H(1,:)/rho;
    vH2 = eH(2,:) + lambda_H(2,:)/rho;
    z_H(1,:) = (vH1 - vH2)/2;
    z_H(2,:) = -z_H(1,:);
    z_H(3,:) = zeros(1,T);

    %% 4c. Primal residual: combined over all 4 energy types
    r_pri = sqrt( norm(eE-z_E,'fro')^2 + norm(eG-z_G,'fro')^2 + ...
                  norm(eH-z_H,'fro')^2 + norm(eC-z_C,'fro')^2 );

    %% 4d. Dual residual: ρ·‖z_new − z_old‖ combined
    r_dual = rho * sqrt( norm(z_E-z_E_old,'fro')^2 + norm(z_G-z_G_old,'fro')^2 + ...
                         norm(z_H-z_H_old,'fro')^2 + norm(z_C-z_C_old,'fro')^2 );

    %% 4e. Multiplier update (gradient ascent on dual variables)
    lambda_E = lambda_E + rho*(eE - z_E);
    lambda_G = lambda_G + rho*(eG - z_G);
    lambda_H = lambda_H + rho*(eH - z_H);
    lambda_C = lambda_C + rho*(eC - z_C);
    lambda_H(3,:) = zeros(1,T);   % Park3 not in heat network — keep at 0

    %% 4f. Adaptive rho
    if r_pri > mu * r_dual
        rho = min(rho_max, omega * rho);
    elseif r_dual > mu * r_pri
        rho = max(rho_min, rho / omega);
    end

    %% 4g. Log
    localObj = sum(cellfun(@(s) s.F_local, last_sol));
    if ~isnan(F_central) && F_central ~= 0
        relGap = (localObj - F_central)/abs(F_central)*100;
    else
        relGap = nan;
    end
    extra(k,:)=[rho_used,max(abs([eE-z_E,eG-z_G,eH-z_H,eC-z_C]),[],'all'),max(abs([sum(eE,1),sum(eG,1),sum(eH,1),sum(eC,1)])),rho==rho_min || rho==rho_max];
    hist(k,:) = [k, r_pri, r_dual, localObj, relGap, rho];
    fprintf('%-6d %10.4f %10.4f %12.4f %9.4f%% %8.3f\n', ...
        k, r_pri, r_dual, localObj, relGap, rho);

    %% 4h. Convergence check
    if r_pri <= eps_pri && r_dual <= eps_dual
        fprintf('\n** Converged at iter %d **\n', k);
        hist = hist(1:k,:);
        break;
    end
end
hist=hist(1:k,:); extra=extra(1:k,:);


elapsed=toc(timer);
runDir=fullfile(thisDir,cfg.id); if ~isfolder(runDir),mkdir(runDir);end
history=array2table([hist,extra],'VariableNames',{'iteration','primal','dual','operating_cost','unused_gap','rho_next','rho_used','max_exchange_mismatch','max_global_balance_error','rho_bound_hit'});
writetable(history,fullfile(runDir,'history.csv'));
result=struct('id',cfg.id,'rho0',cfg.rho0,'mu',cfg.mu,'tau',cfg.tau,'eps',cfg.eps,'converged',r_pri<=eps_pri && r_dual<=eps_dual,'iterations',k,'primal',r_pri,'dual',r_dual,'operating_cost',hist(end,4),'max_exchange_mismatch',extra(end,2),'max_global_balance_error',extra(end,3),'rho_min_seen',min(extra(:,1)),'rho_max_seen',max(extra(:,1)),'rho_bound_hits',sum(extra(:,4)),'elapsed_seconds',elapsed);
fid=fopen(fullfile(runDir,'result.json'),'w');fprintf(fid,'%s',jsonencode(result,PrettyPrint=true));fclose(fid);
save(fullfile(runDir,'raw.mat'),'hist','extra','last_sol','data','p','cap','cfg','z_E','z_G','z_H','z_C','eE','eG','eH','eC','lambda_E','lambda_G','lambda_H','lambda_C');
end
function [p, cap] = get_parameters()
p.e_G3  = 0.26;   p.h_G3   = 0.68;  p.EH    = 0.60;
p.GH    = 0.85;   p.COP_AC = 0.80;  p.COP_EC = 3.00;
p.EG    = 0.60;
p.c_gas        = 0.25;
p.c_buy        = 0.70;
p.c_sell       = 0.20;
p.c_waste_heat = 0.02;
p.ef_gas  = 0.4483;
p.ef_grid = 0.8050;
p.carbon_base_price = 0.45;
p.carbon_growth     = 0.50;
p.carbon_step       = 8;
p.E_quota = [430.0, 530.0, 240.0];

cap.P_G3_min  = [0.00,  0.00,  0.00];
cap.P_G3_max  = [18.00, 25.00, 5.40];
cap.P_EH_max  = [43.00, 70.00, 12.00];
cap.P_GH_max  = [36.00, 52.00, 10.80];
cap.P_AC_max  = [20.00, 28.00,  8.00];
cap.P_EC_max  = [30.00, 25.00, 45.00];
cap.P_EG_max  = [30.00, 10.00, 75.00];
cap.Pbuy_max  = [15.00, 24.00,  8.00];
cap.Psell_min = [-15.00, -8.00, -5.00];

% Net exchange bounds (sum of connected line capacities)
% Electric: E12=8, E13=3, E23=6
cap.eE_max = [8+3, 8+6, 3+6];     % [11, 14, 9]
% Gas: G12=3, G13=4, G23=10
cap.eG_max = [3+4, 3+10, 4+10];   % [7, 13, 14]
% Heat: H12=9 (Park1 & Park2 only, Park3=0)
cap.eH_max = [9, 9, 0];
% Cold: C12=2, C23=2 (C13=0)
cap.eC_max = [2, 2+2, 2];         % [2, 4, 2]
end


% ── solve_park ────────────────────────────────────────────────────────────
% Park m subproblem for 4-energy ADMM.
%
% Objective: F_local(x_m) + Σ_α [λα·(eα-zα) + ρ/2·‖eα-zα‖²]
%
% Local energy balances use net-import variables eα_m:
%   Electric: PWT+PPV+Pnet+PG3 + eE = load_e + PEC/COP_EC + PEG/EG
%   Gas:      Gbuy+PEG + eG        = PG3/e_G3 + PGH/GH + load_g
%   Heat:     PEH+PGH+Hwaste + eH  = load_h + PAC/COP_AC
%   Cold:     PEC+PAC + eC         = load_c
%
% Park3 constraints:
%   eH_3(t) = 0  (not in heat network)
%   eG_3(t) >= -P_EG(3,t)  (gas export only from P2G output)
%
function out = solve_park(m, data, p, cap, ...
        lam_E, lam_G, lam_H, lam_C, ...
        z_E,   z_G,   z_H,   z_C, rho, opts)
T = 24;

%% Decision variables
PG3   = sdpvar(1,T,'full');   PEH   = sdpvar(1,T,'full');
PGH   = sdpvar(1,T,'full');   PAC   = sdpvar(1,T,'full');
PEC   = sdpvar(1,T,'full');   PEG   = sdpvar(1,T,'full');
Pbuy  = sdpvar(1,T,'full');   Psell = sdpvar(1,T,'full');
Gbuy  = sdpvar(1,T,'full');
Hwaste= sdpvar(1,T,'full');   % non-zero only for Park2; bounded to 0 for others

eE = sdpvar(1,T,'full');   % net electric import
eG = sdpvar(1,T,'full');   % net gas import
eH = sdpvar(1,T,'full');   % net heat import
eC = sdpvar(1,T,'full');   % net cold import

carbon_excess = sdpvar(1,1);
carbon_seg    = sdpvar(1,4);

Con = [];

%% Device bounds
for t = 1:T
    Con = [Con, ...
        cap.P_G3_min(m) <= PG3(t),  PG3(t)  <= cap.P_G3_max(m), ...
        0              <= PEH(t),   PEH(t)  <= cap.P_EH_max(m), ...
        0              <= PGH(t),   PGH(t)  <= cap.P_GH_max(m), ...
        0              <= PAC(t),   PAC(t)  <= cap.P_AC_max(m), ...
        0              <= PEC(t),   PEC(t)  <= cap.P_EC_max(m), ...
        0              <= PEG(t),   PEG(t)  <= cap.P_EG_max(m), ...
        0              <= Pbuy(t),  Pbuy(t) <= cap.Pbuy_max(m), ...
        cap.Psell_min(m)<= Psell(t),Psell(t)<= 0, ...
        0              <= Gbuy(t)];

    % CHP waste-heat recovery: inequality (can vent excess heat)
    Con = [Con, PEH(t) <= PG3(t)/p.e_G3 * p.h_G3 * p.EH];

    % Net exchange bounds (line capacity limits)
    Con = [Con, -cap.eE_max(m) <= eE(t), eE(t) <= cap.eE_max(m)];
    Con = [Con, -cap.eG_max(m) <= eG(t), eG(t) <= cap.eG_max(m)];
    Con = [Con, -cap.eH_max(m) <= eH(t), eH(t) <= cap.eH_max(m)];
    Con = [Con, -cap.eC_max(m) <= eC(t), eC(t) <= cap.eC_max(m)];

    % Waste heat bounds (Park2 only; fixed 0 for others)
    if m == 2
        Con = [Con, 0 <= Hwaste(t), Hwaste(t) <= data.waste_heat_avail(t)];
    else
        Con = [Con, Hwaste(t) == 0];
    end

    % Park3: heat not connected → force eH=0
    if m == 3
        Con = [Con, eH(t) == 0];
    end
end

%% Park3: P2G gas export constraint
% Gas export (-eG_3) must not exceed P2G output: eG_3 >= -PEG(t)
if m == 3
    for t = 1:T
        Con = [Con, eG(t) >= -PEG(t)];
    end
end

%% Energy balance constraints (using net import variables)
for t = 1:T
    % Electric
    Con = [Con, data.P_WT(m,t) + data.P_PV(m,t) + Pbuy(t) + Psell(t) ...
        + PG3(t) + eE(t) - PEC(t)/p.COP_EC - PEG(t)/p.EG == data.load_e(m,t)];
    % Gas
    Con = [Con, Gbuy(t) + PEG(t) + eG(t) == ...
        PG3(t)/p.e_G3 + PGH(t)/p.GH + data.load_g(m,t)];
    % Heat (Hwaste = 0 for Park1 and Park3)
    Con = [Con, PEH(t) + PGH(t) + Hwaste(t) + eH(t) == ...
        data.load_h(m,t) + PAC(t)/p.COP_AC];
    % Cold
    Con = [Con, PEC(t) + PAC(t) + eC(t) == data.load_c(m,t)];
end

%% Carbon trading (4-tier stepped)
emission = p.ef_gas*sum(Gbuy) + p.ef_grid*sum(Pbuy);
cp = p.carbon_base_price * [1, 1+p.carbon_growth, 1+2*p.carbon_growth, 1+3*p.carbon_growth];
Con = [Con, carbon_excess >= 0, carbon_excess >= emission - p.E_quota(m)];
Con = [Con, carbon_excess == sum(carbon_seg), carbon_seg >= 0];
Con = [Con, carbon_seg(1)<=p.carbon_step, carbon_seg(2)<=p.carbon_step, carbon_seg(3)<=p.carbon_step];

%% Local cost (no exchange transaction cost — captured by Lagrangian)
C_gas    = p.c_gas   * sum(Gbuy);
C_buy    = p.c_buy   * sum(Pbuy);
C_sell   = p.c_sell  * sum(Psell);
C_carbon = cp * carbon_seg';
C_waste  = p.c_waste_heat * sum(Hwaste);
F_local  = C_gas + C_buy + C_sell + C_carbon + C_waste;

%% ADMM augmented Lagrangian coupling (all 4 energy types)
lag = lam_E*(eE-z_E)' + rho/2*sum((eE-z_E).^2) ...
    + lam_G*(eG-z_G)' + rho/2*sum((eG-z_G).^2) ...
    + lam_H*(eH-z_H)' + rho/2*sum((eH-z_H).^2) ...
    + lam_C*(eC-z_C)' + rho/2*sum((eC-z_C).^2);

%% Solve
result = optimize(Con, F_local + lag, opts);

%% Extract
out.problem = result.problem;
out.info    = result.info;
if result.problem == 0
    out.eE = value(eE);  out.eG = value(eG);
    out.eH = value(eH);  out.eC = value(eC);
    out.PG3 = value(PG3);  out.PEH = value(PEH);
    out.PGH = value(PGH);  out.PAC = value(PAC);
    out.PEC = value(PEC);  out.PEG = value(PEG);
    out.Pbuy  = value(Pbuy);   out.Psell = value(Psell);
    out.Gbuy  = value(Gbuy);   out.Hwaste = value(Hwaste);
    out.F_local  = value(F_local);
    out.C_energy = value(C_gas + C_buy + C_sell);
    out.C_carbon = value(C_carbon);
    out.emission = value(emission);
    out.carbon_excess = value(carbon_excess);
else
    out.eE=nan(1,T); out.eG=nan(1,T); out.eH=nan(1,T); out.eC=nan(1,T);
    flds = {'PG3','PEH','PGH','PAC','PEC','PEG','Pbuy','Psell','Gbuy','Hwaste'};
    for i=1:length(flds); out.(flds{i})=nan(1,T); end
    out.F_local=nan; out.C_energy=nan; out.C_carbon=nan;
    out.emission=nan; out.carbon_excess=nan;
end
end
