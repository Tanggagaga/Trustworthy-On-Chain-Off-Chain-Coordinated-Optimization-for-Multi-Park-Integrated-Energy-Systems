function [Con,F_local,v]=original_local_model(m,data,p,cap)
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

names={'PG3','PEH','PGH','PAC','PEC','PEG','Pbuy','Psell','Gbuy','Hwaste','eE','eG','eH','eC','carbon_excess','carbon_seg'};
v=struct();
for j=1:numel(names)
    v.(names{j})=eval(names{j});
end
end
