% export_all_park_masks.m
%
% 为M6完整47轮闭环导出三个园区的注册量(H_e0,G_masked,h_masked,C0,E_rows,x0_e)。
% 跟M1a/M3/M4同rng(42)种子、同构造顺序，结果应完全复现（已反复验证过）。
% 这次三个园区都要导出（之前M4真实数据校验只导出过Park1）。

clc; clear; rng(42);

thisDir   = fileparts(mfilename('fullpath'));
designDir = fullfile(fileparts(thisDir), 'case');
parkFiles = {
    'park1_commercial_integrated_timeseries.csv'
    'park2_industrial_waste_heat_timeseries.csv'
    'park3_wind_p2g_export_timeseries.csv'
};
M = 3; T = 24;
data.load_e=zeros(M,T); data.load_g=zeros(M,T); data.load_h=zeros(M,T); data.load_c=zeros(M,T);
data.P_PV=zeros(M,T); data.P_WT=zeros(M,T); data.waste_heat_avail=zeros(1,T);
for m=1:M
    tbl=readtable(fullfile(designDir,parkFiles{m}));
    data.load_e(m,:)=tbl.load_e'; data.load_g(m,:)=tbl.load_g';
    data.load_h(m,:)=tbl.load_h'; data.load_c(m,:)=tbl.load_c';
    data.P_PV(m,:)=tbl.P_PV'; data.P_WT(m,:)=tbl.P_WT';
    if m==2; data.waste_heat_avail=tbl.waste_heat_available'; end
end
[p, cap] = get_parameters();

MASK_SCALE = 1e6;
toFixed = @(x) int64(round(x * MASK_SCALE));
toFixedMat = @(M_) arrayfun(@(i) toFixed(M_(i,:)), 1:size(M_,1), 'UniformOutput', false)';

for m = 1:M
    [~, eqExprs, ineqExprs, xvec, idx_e, c_d_m, ~] = build_park_constraints(m, data, p, cap);
    eqVec = vertcat(eqExprs{:}); ineqVec = vertcat(ineqExprs{:});
    A_m = full(double(jacobian(eqVec,xvec)));   b_m = full(-double(replace(eqVec,xvec,zeros(size(xvec)))));
    G_m = full(double(jacobian(ineqVec,xvec))); h_m = full(-double(replace(ineqVec,xvec,zeros(size(xvec)))));
    n = numel(xvec);
    D_e = zeros(n,n); D_e(idx_e,idx_e)=eye(numel(idx_e));
    mask = build_mask(A_m,b_m,G_m,h_m,D_e,double(c_d_m),idx_e,1e-8);
    fprintf('Park%d mask: rank(A)=%d k0=%d (应与M1a/M3/M4一致)\n', m, mask.rankA, mask.k0);

    bundle = struct();
    bundle.park = m;
    bundle.k0 = mask.k0;
    bundle.qRows = size(mask.G_masked,1);
    bundle.maskScale = MASK_SCALE;
    bundle.hE0Scaled = toFixedMat(mask.H_e0);
    bundle.gMaskedScaled = toFixedMat(mask.G_masked);
    bundle.hVecScaled = toFixed(mask.h_masked)';
    bundle.c0Scaled = toFixed(mask.C0)';
    bundle.eRowsScaled = toFixedMat(mask.E_m);
    bundle.x0eScaled = toFixed(mask.x0_e)';

    outPath = fullfile(thisDir, sprintf('mask_registration_park%d.json', m));
    fid = fopen(outPath, 'w');
    fprintf(fid, '%s', jsonencode(bundle));
    fclose(fid);
    fprintf('已导出: %s\n', outPath);
end


%% =========================================================================
function [p, cap] = get_parameters()
p.e_G3=0.26; p.h_G3=0.68; p.EH=0.60; p.GH=0.85; p.COP_AC=0.80; p.COP_EC=3.00; p.EG=0.60;
p.c_gas=0.25; p.c_buy=0.70; p.c_sell=0.20; p.c_waste_heat=0.02;
p.ef_gas=0.4483; p.ef_grid=0.8050;
p.carbon_base_price=0.45; p.carbon_growth=0.50; p.carbon_step=8;
p.E_quota=[430.0,530.0,240.0];
cap.P_G3_min=[0.00,0.00,0.00]; cap.P_G3_max=[18.00,25.00,5.40];
cap.P_EH_max=[43.00,70.00,12.00]; cap.P_GH_max=[36.00,52.00,10.80];
cap.P_AC_max=[20.00,28.00,8.00]; cap.P_EC_max=[30.00,25.00,45.00];
cap.P_EG_max=[30.00,10.00,75.00]; cap.Pbuy_max=[15.00,24.00,8.00];
cap.Psell_min=[-15.00,-8.00,-5.00];
cap.eE_max=[8+3,8+6,3+6]; cap.eG_max=[3+4,3+10,4+10];
cap.eH_max=[9,9,0]; cap.eC_max=[2,2+2,2];
end

function [Con, eqExprs, ineqExprs, xvec, idx_e, c_d_m, vars] = build_park_constraints(m, data, p, cap)
T=24;
PG3=sdpvar(1,T,'full'); PEH=sdpvar(1,T,'full'); PGH=sdpvar(1,T,'full');
PAC=sdpvar(1,T,'full'); PEC=sdpvar(1,T,'full'); PEG=sdpvar(1,T,'full');
Pbuy=sdpvar(1,T,'full'); Psell=sdpvar(1,T,'full'); Gbuy=sdpvar(1,T,'full');
Hwaste=sdpvar(1,T,'full');
eE=sdpvar(1,T,'full'); eG=sdpvar(1,T,'full'); eH=sdpvar(1,T,'full'); eC=sdpvar(1,T,'full');
carbon_excess=sdpvar(1,1); carbon_seg=sdpvar(1,4);
Con=[]; eqExprs={}; ineqExprs={};
for t=1:T
    ineqExprs{end+1}=cap.P_G3_min(m)-PG3(t); Con=[Con,cap.P_G3_min(m)<=PG3(t)]; %#ok<AGROW>
    ineqExprs{end+1}=PG3(t)-cap.P_G3_max(m); Con=[Con,PG3(t)<=cap.P_G3_max(m)]; %#ok<AGROW>
    ineqExprs{end+1}=-PEH(t); Con=[Con,0<=PEH(t)]; %#ok<AGROW>
    ineqExprs{end+1}=PEH(t)-cap.P_EH_max(m); Con=[Con,PEH(t)<=cap.P_EH_max(m)]; %#ok<AGROW>
    ineqExprs{end+1}=-PGH(t); Con=[Con,0<=PGH(t)]; %#ok<AGROW>
    ineqExprs{end+1}=PGH(t)-cap.P_GH_max(m); Con=[Con,PGH(t)<=cap.P_GH_max(m)]; %#ok<AGROW>
    ineqExprs{end+1}=-PAC(t); Con=[Con,0<=PAC(t)]; %#ok<AGROW>
    ineqExprs{end+1}=PAC(t)-cap.P_AC_max(m); Con=[Con,PAC(t)<=cap.P_AC_max(m)]; %#ok<AGROW>
    ineqExprs{end+1}=-PEC(t); Con=[Con,0<=PEC(t)]; %#ok<AGROW>
    ineqExprs{end+1}=PEC(t)-cap.P_EC_max(m); Con=[Con,PEC(t)<=cap.P_EC_max(m)]; %#ok<AGROW>
    ineqExprs{end+1}=-PEG(t); Con=[Con,0<=PEG(t)]; %#ok<AGROW>
    ineqExprs{end+1}=PEG(t)-cap.P_EG_max(m); Con=[Con,PEG(t)<=cap.P_EG_max(m)]; %#ok<AGROW>
    ineqExprs{end+1}=-Pbuy(t); Con=[Con,0<=Pbuy(t)]; %#ok<AGROW>
    ineqExprs{end+1}=Pbuy(t)-cap.Pbuy_max(m); Con=[Con,Pbuy(t)<=cap.Pbuy_max(m)]; %#ok<AGROW>
    ineqExprs{end+1}=cap.Psell_min(m)-Psell(t); Con=[Con,cap.Psell_min(m)<=Psell(t)]; %#ok<AGROW>
    ineqExprs{end+1}=Psell(t); Con=[Con,Psell(t)<=0]; %#ok<AGROW>
    ineqExprs{end+1}=-Gbuy(t); Con=[Con,0<=Gbuy(t)]; %#ok<AGROW>
    ineqExprs{end+1}=PEH(t)-PG3(t)/p.e_G3*p.h_G3*p.EH; %#ok<AGROW>
    Con=[Con,PEH(t)<=PG3(t)/p.e_G3*p.h_G3*p.EH];
    if m ~= 3
        ineqExprs{end+1}=-cap.eH_max(m)-eH(t); Con=[Con,-cap.eH_max(m)<=eH(t)]; %#ok<AGROW>
        ineqExprs{end+1}=eH(t)-cap.eH_max(m); Con=[Con,eH(t)<=cap.eH_max(m)]; %#ok<AGROW>
    end
    ineqExprs{end+1}=-cap.eE_max(m)-eE(t); Con=[Con,-cap.eE_max(m)<=eE(t)]; %#ok<AGROW>
    ineqExprs{end+1}=eE(t)-cap.eE_max(m); Con=[Con,eE(t)<=cap.eE_max(m)]; %#ok<AGROW>
    ineqExprs{end+1}=-cap.eG_max(m)-eG(t); Con=[Con,-cap.eG_max(m)<=eG(t)]; %#ok<AGROW>
    ineqExprs{end+1}=eG(t)-cap.eG_max(m); Con=[Con,eG(t)<=cap.eG_max(m)]; %#ok<AGROW>
    ineqExprs{end+1}=-cap.eC_max(m)-eC(t); Con=[Con,-cap.eC_max(m)<=eC(t)]; %#ok<AGROW>
    ineqExprs{end+1}=eC(t)-cap.eC_max(m); Con=[Con,eC(t)<=cap.eC_max(m)]; %#ok<AGROW>
    if m==2
        ineqExprs{end+1}=-Hwaste(t); Con=[Con,0<=Hwaste(t)]; %#ok<AGROW>
        ineqExprs{end+1}=Hwaste(t)-data.waste_heat_avail(t); Con=[Con,Hwaste(t)<=data.waste_heat_avail(t)]; %#ok<AGROW>
    else
        eqExprs{end+1}=Hwaste(t); Con=[Con,Hwaste(t)==0]; %#ok<AGROW>
    end
    if m==3
        eqExprs{end+1}=eH(t); Con=[Con,eH(t)==0]; %#ok<AGROW>
    end
end
if m==3
    for t=1:T
        ineqExprs{end+1}=-PEG(t)-eG(t); Con=[Con,eG(t)>=-PEG(t)]; %#ok<AGROW>
    end
end
for t=1:T
    elecLHS=data.P_WT(m,t)+data.P_PV(m,t)+Pbuy(t)+Psell(t)+PG3(t)+eE(t)-PEC(t)/p.COP_EC-PEG(t)/p.EG;
    eqExprs{end+1}=elecLHS-data.load_e(m,t); Con=[Con,elecLHS==data.load_e(m,t)]; %#ok<AGROW>
    gasLHS=Gbuy(t)+PEG(t)+eG(t); gasRHS=PG3(t)/p.e_G3+PGH(t)/p.GH+data.load_g(m,t);
    eqExprs{end+1}=gasLHS-gasRHS; Con=[Con,gasLHS==gasRHS]; %#ok<AGROW>
    heatLHS=PEH(t)+PGH(t)+Hwaste(t)+eH(t); heatRHS=data.load_h(m,t)+PAC(t)/p.COP_AC;
    eqExprs{end+1}=heatLHS-heatRHS; Con=[Con,heatLHS==heatRHS]; %#ok<AGROW>
    coldLHS=PEC(t)+PAC(t)+eC(t);
    eqExprs{end+1}=coldLHS-data.load_c(m,t); Con=[Con,coldLHS==data.load_c(m,t)]; %#ok<AGROW>
end
emission=p.ef_gas*sum(Gbuy)+p.ef_grid*sum(Pbuy);
ineqExprs{end+1}=-carbon_excess; Con=[Con,carbon_excess>=0]; %#ok<AGROW>
ineqExprs{end+1}=emission-p.E_quota(m)-carbon_excess; Con=[Con,carbon_excess>=emission-p.E_quota(m)]; %#ok<AGROW>
eqExprs{end+1}=carbon_excess-sum(carbon_seg); Con=[Con,carbon_excess==sum(carbon_seg)]; %#ok<AGROW>
for i=1:4
    ineqExprs{end+1}=-carbon_seg(i); Con=[Con,carbon_seg(i)>=0]; %#ok<AGROW>
end
ineqExprs{end+1}=carbon_seg(1)-p.carbon_step; Con=[Con,carbon_seg(1)<=p.carbon_step]; %#ok<AGROW>
ineqExprs{end+1}=carbon_seg(2)-p.carbon_step; Con=[Con,carbon_seg(2)<=p.carbon_step]; %#ok<AGROW>
ineqExprs{end+1}=carbon_seg(3)-p.carbon_step; Con=[Con,carbon_seg(3)<=p.carbon_step]; %#ok<AGROW>
xvec=[PG3,PEH,PGH,PAC,PEC,PEG,Pbuy,Psell,Gbuy,Hwaste,eE,eG,eH,eC,carbon_excess,carbon_seg];
idx_e=241:336;
cp=p.carbon_base_price*[1,1+p.carbon_growth,1+2*p.carbon_growth,1+3*p.carbon_growth];
F_local=p.c_gas*sum(Gbuy)+p.c_buy*sum(Pbuy)+p.c_sell*sum(Psell)+cp*carbon_seg'+p.c_waste_heat*sum(Hwaste);
c_d_m=jacobian(F_local,xvec);
vars=struct('emission',emission,'F_local',F_local);
end

function mask = build_mask(A_m,b_m,G_m,h_m,D_e,c_d_m_row,idx_e,tol)
N_m=null(A_m); x0=lsqminnorm(A_m,b_m); resid=norm(A_m*x0-b_m);
if resid>tol, error('x0 residual %.3e exceeds tol',resid); end
k0=size(N_m,2); [R_m,~]=qr(randn(k0));
H_e0=R_m'*(N_m'*D_e*N_m)*R_m; G_masked=G_m*N_m*R_m; h_masked=h_m-G_m*x0;
C0=c_d_m_row*N_m*R_m; E_m=N_m(idx_e,:)*R_m; x0_e=x0(idx_e);
mask=struct('N',N_m,'R',R_m,'x0',x0,'k0',k0,'n',size(A_m,2),'H_e0',H_e0,'G_masked',G_masked, ...
    'h_masked',h_masked,'C0',C0,'E_m',E_m,'x0_e',x0_e,'resid',resid,'rankA',rank(A_m));
end
