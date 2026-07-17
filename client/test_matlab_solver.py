"""Smoke test for the MATLAB/Gurobi bridge."""

import matlab_solver


def main():
    res = matlab_solver.solve_park(1, [0.0] * 24, [0.0] * 24, 1.0)
    print("problem", res["problem"])
    print("info", res["info"])
    print("local_cost", round(res["local_cost"], 6))
    print("local_carbon", round(res["local_carbon"], 6))
    print("e_first3", [round(x, 6) for x in res["e_ex"][:3]])
    print("e_len", len(res["e_ex"]))

    rows = [[0.0] * 24, [0.0] * 24, [0.0] * 24]
    all_res = matlab_solver.solve_all_parks(rows, rows, 1.0)
    print("all_count", len(all_res))
    for item in all_res:
        print(
            "park",
            item["park_id"],
            "problem",
            item["problem"],
            "cost",
            round(item["local_cost"], 6),
            "e0",
            round(item["e_ex"][0], 6),
        )


if __name__ == "__main__":
    main()
