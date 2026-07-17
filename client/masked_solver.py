"""
masked QP 求解器 + 链下KKT校验器。

关键设计：这个模块只处理 masked 问题（H_masked, c_masked, G_masked, h_masked），
全部是公开数据（链上注册量 + 链上λ,z,ρ 推出来的），不需要、也不接触任何
园区的真实私有数据（负荷、设备容量、成本系数）——这正是 mask 机制要做到的：
求解方不需要看懂物理含义就能解题。

求解用 OSQP（轻量QP求解器，原生支持对偶变量输出），不需要MATLAB/Gurobi，
对偶变量符号约定已经跟 M2/M3 的 KKT 公式核对过（H'y*+c'+G'^T*mu*=0, mu*>=0），
一致，不需要翻转。
"""

import math

import numpy as np
import scipy.sparse as sp
import osqp

MASK_SCALE = 1_000_000
KKT_TOL = int(0.01 * MASK_SCALE)


def build_masked_round(mask, rho, lam_e_block, z_e_block):
    """
    用注册量(mask, 已反缩放为float) + 当轮公开的 λ,z,ρ，机械算出本轮的
    H_masked^(k), c_masked^(k)。跟链码/M1a/M3用的同一套公式：
        H_masked = rho * H_e0
        c_masked = C0 + rho*(x0_e · E_rows) + (lambda - rho*z) · E_rows
    """
    h_e0 = np.array(mask["H_e0"])
    c0 = np.array(mask["C0"])
    e_rows = np.array(mask["E_rows"])  # 96 x k0
    x0_e = np.array(mask["x0_e"])      # 96

    h_masked = rho * h_e0
    lam = np.array(lam_e_block)
    z = np.array(z_e_block)
    c_masked = c0 + rho * (x0_e @ e_rows) + (lam - rho * z) @ e_rows
    return h_masked, c_masked


def solve_masked_qp(h_masked, c_masked, g_masked, h_vec):
    """
    解 min 0.5 y'Hy + c'y  s.t. G y <= h。
    返回 (y*, mu*)，均为float（未缩放）。
    """
    k0 = h_masked.shape[0]
    P = sp.csc_matrix(h_masked)
    A = sp.csc_matrix(g_masked)
    l = -np.inf * np.ones(len(h_vec))
    u = np.array(h_vec, dtype=float)

    m = osqp.OSQP()
    m.setup(P=P, q=c_masked, A=A, l=l, u=u, verbose=False,
            eps_abs=1e-9, eps_rel=1e-9, max_iter=20000, polish=True)
    res = m.solve()
    if res.info.status_val not in (1, 2):  # 1=solved, 2=solved inaccurate
        raise RuntimeError(f"OSQP求解失败: {res.info.status}")
    return res.x, res.y


def divround(num, den):
    """跟链码/M3.5一致的四舍五入除法（整数版）。"""
    if den == 0:
        raise ZeroDivisionError
    if den < 0:
        num, den = -num, -den
    sign = -1 if num < 0 else 1
    n = abs(num)
    q, r = divmod(n, den)
    if r * 2 >= den:
        q += 1
    return sign * q


def check_kkt_scaled(h_masked_scaled, c_masked_scaled, g_masked_scaled, h_vec_scaled,
                      y_scaled, mu_scaled, tol=KKT_TOL):
    """
    跟M2/M3.5完全一致的四条KKT校验，纯定点整数实现，供verifier角色调用。
    输入全部是按MASK_SCALE定点化的int64（或可转成int的值）。
    """
    k0 = len(y_scaled)
    qrows = len(mu_scaled)

    hy = [sum(divround(h_masked_scaled[i][j] * y_scaled[j], MASK_SCALE) for j in range(k0))
          for i in range(k0)]
    gtmu = [sum(divround(g_masked_scaled[r][i] * mu_scaled[r], MASK_SCALE) for r in range(qrows))
            for i in range(k0)]
    stat = [hy[i] + c_masked_scaled[i] + gtmu[i] for i in range(k0)]
    stat_res = int(round(math.sqrt(sum(s * s for s in stat))))

    gy = [sum(divround(g_masked_scaled[r][j] * y_scaled[j], MASK_SCALE) for j in range(k0))
          for r in range(qrows)]
    slack = [gy[r] - h_vec_scaled[r] for r in range(qrows)]
    compl = [abs(divround(mu_scaled[r] * slack[r], MASK_SCALE)) for r in range(qrows)]
    compl_res = max(compl) if compl else 0

    primal_ok = all(s <= tol for s in slack)
    dual_ok = all(mu >= -tol for mu in mu_scaled)
    stat_ok = stat_res <= tol
    compl_ok = compl_res <= tol
    passed = primal_ok and dual_ok and stat_ok and compl_ok
    return passed, stat_res, compl_res
