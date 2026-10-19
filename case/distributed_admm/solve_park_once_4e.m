function solve_park_once_4e(input_json, output_json)
%SOLVE_PARK_ONCE_4E  Solve one park's 4-energy ADMM subproblem and write JSON result.
%
% Called from Python via MATLAB -batch mode.
%
% Input JSON fields:
%   park_id  : 1, 2, or 3
%   lambda_E, lambda_G, lambda_H, lambda_C : 1×24 double arrays
%   z_E,      z_G,      z_H,      z_C      : 1×24 double arrays
%   rho      : positive scalar
%
% Output JSON fields:
%   park_id, problem, info
%   eE, eG, eH, eC : 1×24 double arrays (net import per energy type)
%   local_cost      : F_local (scalar)
%   local_carbon    : emission (scalar, tCO2)
%   C_energy        : energy cost component
%   C_carbon        : carbon trading cost component

%% ── Read and validate input ─────────────────────────────────────────────────
raw = fileread(input_json);
req = jsondecode(raw);

m       = double(req.park_id);
lam_E   = reshape(double(req.lambda_E), 1, []);
lam_G   = reshape(double(req.lambda_G), 1, []);
lam_H   = reshape(double(req.lambda_H), 1, []);
lam_C   = reshape(double(req.lambda_C), 1, []);
z_E     = reshape(double(req.z_E),      1, []);
z_G     = reshape(double(req.z_G),      1, []);
z_H     = reshape(double(req.z_H),      1, []);
z_C     = reshape(double(req.z_C),      1, []);
rho     = double(req.rho);

if ~ismember(m, [1 2 3])
    error('park_id must be 1, 2, or 3.');
end
T = 24;
for fld = {'lam_E','lam_G','lam_H','lam_C','z_E','z_G','z_H','z_C'}
    if numel(eval(fld{1})) ~= T
        error('%s must have %d elements.', fld{1}, T);
    end
end
if rho <= 0
    error('rho must be positive.');
end

%% ── Load data ───────────────────────────────────────────────────────────────
thisDir   = fileparts(mfilename('fullpath'));
designDir = fileparts(thisDir);   % 新_三个园区设计/

parkFiles = {
    'park1_commercial_integrated_timeseries.csv'
    'park2_industrial_waste_heat_timeseries.csv'
    'park3_wind_p2g_export_timeseries.csv'
};

% Load all three parks so index m works directly (matches main ADMM script).
M = 3;
data.load_e          = zeros(M, T);
data.load_g          = zeros(M, T);
data.load_h          = zeros(M, T);
data.load_c          = zeros(M, T);
data.P_PV            = zeros(M, T);
data.P_WT            = zeros(M, T);
data.waste_heat_avail = zeros(1, T);   % only non-zero for Park2

for i = 1:M
    tbl = readtable(fullfile(designDir, parkFiles{i}));
    data.load_e(i,:) = tbl.load_e';
    data.load_g(i,:) = tbl.load_g';
    data.load_h(i,:) = tbl.load_h';
    data.load_c(i,:) = tbl.load_c';
    data.P_PV(i,:)   = tbl.P_PV';
    data.P_WT(i,:)   = tbl.P_WT';
    if i == 2
        data.waste_heat_avail = tbl.waste_heat_available';
    end
end

%% ── Solve subproblem ────────────────────────────────────────────────────────
[p, cap] = local_get_parameters();
opts = sdpsettings('solver', 'gurobi', 'verbose', 0);

sol = local_solve_park(m, data, p, cap, ...
    lam_E, lam_G, lam_H, lam_C, ...
    z_E,   z_G,   z_H,   z_C,   rho, opts);

%% ── Write output ─────────────────────────────────────────────────────────────
res.park_id      = m;
res.problem      = sol.problem;
res.info         = sol.info;
res.eE           = sol.eE;
res.eG           = sol.eG;
res.eH           = sol.eH;
res.eC           = sol.eC;
res.local_cost   = sol.F_local;
res.local_carbon = sol.emission;
res.C_energy     = sol.C_energy;
res.C_carbon     = sol.C_carbon;

fid = fopen(output_json, 'w');
if fid < 0
    error('Cannot open output JSON: %s', output_json);
end
cleanup = onCleanup(@() fclose(fid));
fprintf(fid, '%s', jsonencode(res));
end


%% =========================================================================
%% LOCAL FUNCTIONS  (copied verbatim from admm_four_energy_three_park.m)
%% =========================================================================

function [p, cap] = local_get_parameters()
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

% Net exchange capacity bounds (sum of connected line capacities)
% Electric: E12=8, E13=3, E23=6
cap.eE_max = [8+3, 8+6, 3+6];     % [11, 14, 9]
% Gas: G12=3, G13=4, G23=10
cap.eG_max = [3+4, 3+10, 4+10];   % [7, 13, 14]
% Heat: H12=9 (Park1-Park2 only; Park3 = 0 by topology)
cap.eH_max = [9, 9, 0];
% Cold: C12=2, C23=2 (C13=0)
cap.eC_max = [2, 2+2, 2];         % [2, 4, 2]
end


function out = local_solve_park(m, data, p, cap, ...
        lam_E, lam_G, lam_H, lam_C, ...
        z_E,   z_G,   z_H,   z_C, rho, opts)
% Park m subproblem for 4-energy ADMM.
%
% Objective: F_local(x_m) + Σ_α [λα·(eα-zα) + ρ/2·‖eα-zα‖²]
%
% Energy balances (net-import formulation):
%   Electric: PWT+PPV+Pbuy+Psell + PG3 + eE = load_e + PEC/COP_EC + PEG/EG
%   Gas:      Gbuy + PEG + eG              = PG3/e_G3 + PGH/GH + load_g
%   Heat:     PEH + PGH + Hwaste + eH      = load_h + PAC/COP_AC
%   Cold:     PEC + PAC + eC               = load_c
%
% Park3 special constraints:
%   eH_3(t) = 0          (not connected to heat network)
%   eG_3(t) >= -PEG_3(t) (gas export only from P2G output, not external gas)

T = 24;

%% Decision variables
PG3    = sdpvar(1,T,'full');
PEH    = sdpvar(1,T,'full');
PGH    = sdpvar(1,T,'full');
PAC    = sdpvar(1,T,'full');
PEC    = sdpvar(1,T,'full');
PEG    = sdpvar(1,T,'full');
Pbuy   = sdpvar(1,T,'full');
Psell  = sdpvar(1,T,'full');
Gbuy   = sdpvar(1,T,'full');
Hwaste = sdpvar(1,T,'full');

eE = sdpvar(1,T,'full');
eG = sdpvar(1,T,'full');
eH = sdpvar(1,T,'full');
eC = sdpvar(1,T,'full');

carbon_excess = sdpvar(1,1);
carbon_seg    = sdpvar(1,4);

Con = [];

%% Device bounds and per-hour constraints
for t = 1:T
    Con = [Con, ...
        cap.P_G3_min(m) <= PG3(t),  PG3(t)  <= cap.P_G3_max(m), ...
        0               <= PEH(t),  PEH(t)  <= cap.P_EH_max(m), ...
        0               <= PGH(t),  PGH(t)  <= cap.P_GH_max(m), ...
        0               <= PAC(t),  PAC(t)  <= cap.P_AC_max(m), ...
        0               <= PEC(t),  PEC(t)  <= cap.P_EC_max(m), ...
        0               <= PEG(t),  PEG(t)  <= cap.P_EG_max(m), ...
        0               <= Pbuy(t), Pbuy(t) <= cap.Pbuy_max(m), ...
        cap.Psell_min(m)<= Psell(t),Psell(t)<= 0, ...
        0               <= Gbuy(t)];

    % CHP waste-heat recovery bound (inequality: can vent excess)
    Con = [Con, PEH(t) <= PG3(t)/p.e_G3 * p.h_G3 * p.EH];

    % Net exchange bounds (aggregated line capacity)
    Con = [Con, -cap.eE_max(m) <= eE(t), eE(t) <= cap.eE_max(m)];
    Con = [Con, -cap.eG_max(m) <= eG(t), eG(t) <= cap.eG_max(m)];
    Con = [Con, -cap.eH_max(m) <= eH(t), eH(t) <= cap.eH_max(m)];
    Con = [Con, -cap.eC_max(m) <= eC(t), eC(t) <= cap.eC_max(m)];

    % Waste-heat recovery: only Park2 has industrial waste heat
    if m == 2
        Con = [Con, 0 <= Hwaste(t), Hwaste(t) <= data.waste_heat_avail(t)];
    else
        Con = [Con, Hwaste(t) == 0];
    end

    % Park3: not connected to heat network
    if m == 3
        Con = [Con, eH(t) == 0];
    end
end

%% Park3: P2G gas export constraint
% Gas export from Park3 must originate from P2G output, not external gas
if m == 3
    for t = 1:T
        Con = [Con, eG(t) >= -PEG(t)];
    end
end

%% Energy balance constraints
for t = 1:T
    % Electric balance
    Con = [Con, data.P_WT(m,t) + data.P_PV(m,t) + Pbuy(t) + Psell(t) ...
        + PG3(t) + eE(t) - PEC(t)/p.COP_EC - PEG(t)/p.EG == data.load_e(m,t)];
    % Gas balance
    Con = [Con, Gbuy(t) + PEG(t) + eG(t) == ...
        PG3(t)/p.e_G3 + PGH(t)/p.GH + data.load_g(m,t)];
    % Heat balance
    Con = [Con, PEH(t) + PGH(t) + Hwaste(t) + eH(t) == ...
        data.load_h(m,t) + PAC(t)/p.COP_AC];
    % Cold balance
    Con = [Con, PEC(t) + PAC(t) + eC(t) == data.load_c(m,t)];
end

%% Carbon trading (4-tier stepped price)
emission = p.ef_gas * sum(Gbuy) + p.ef_grid * sum(Pbuy);
cp = p.carbon_base_price * [1, 1+p.carbon_growth, 1+2*p.carbon_growth, 1+3*p.carbon_growth];
Con = [Con, carbon_excess >= 0, carbon_excess >= emission - p.E_quota(m)];
Con = [Con, carbon_excess == sum(carbon_seg), carbon_seg >= 0];
Con = [Con, carbon_seg(1) <= p.carbon_step, carbon_seg(2) <= p.carbon_step, ...
            carbon_seg(3) <= p.carbon_step];

%% Local cost (no inter-park transaction cost — handled by Lagrangian)
C_gas    = p.c_gas        * sum(Gbuy);
C_buy    = p.c_buy        * sum(Pbuy);
C_sell   = p.c_sell       * sum(Psell);
C_carbon = cp             * carbon_seg';
C_waste  = p.c_waste_heat * sum(Hwaste);
F_local  = C_gas + C_buy + C_sell + C_carbon + C_waste;

%% ADMM augmented Lagrangian coupling terms (all 4 energy types)
lag = lam_E*(eE-z_E)' + rho/2*sum((eE-z_E).^2) ...
    + lam_G*(eG-z_G)' + rho/2*sum((eG-z_G).^2) ...
    + lam_H*(eH-z_H)' + rho/2*sum((eH-z_H).^2) ...
    + lam_C*(eC-z_C)' + rho/2*sum((eC-z_C).^2);

%% Solve
result = optimize(Con, F_local + lag, opts);

%% Extract results
out.problem = result.problem;
out.info    = result.info;
if result.problem == 0
    out.eE       = value(eE);
    out.eG       = value(eG);
    out.eH       = value(eH);
    out.eC       = value(eC);
    out.F_local  = value(F_local);
    out.C_energy = value(C_gas + C_buy + C_sell);
    out.C_carbon = value(C_carbon);
    out.emission = value(emission);
    out.carbon_excess = value(carbon_excess);
    out.Gbuy     = value(Gbuy);
    out.Pbuy     = value(Pbuy);
    out.PG3      = value(PG3);
    out.PEG      = value(PEG);
    out.PEC      = value(PEC);
else
    out.eE = nan(1,T);  out.eG = nan(1,T);
    out.eH = nan(1,T);  out.eC = nan(1,T);
    out.F_local  = nan;  out.C_energy = nan;
    out.C_carbon = nan;  out.emission = nan;
    out.carbon_excess = nan;
    out.Gbuy = nan(1,T);  out.Pbuy = nan(1,T);
    out.PG3  = nan(1,T);  out.PEG  = nan(1,T);
    out.PEC  = nan(1,T);
end
end
