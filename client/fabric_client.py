"""
封装所有 peer CLI 调用。

核心设计原则：
  caller_org    → 决定 GetMSPID()，用于链码内部身份校验
  endorsing_orgs → 决定背书Peer，必须满足链码级 2/3 Park 策略

链码级策略：OutOf(2, Park1MSP.peer, Park2MSP.peer, Park3MSP.peer)
所有写交易的 endorsing_orgs 必须包含至少2个Park。
"""

import json
import os
import subprocess
import time

from config import (
    CHANNEL_NAME, CHAINCODE_NAME, CFG_PATH, FAB_BIN,
    ORDERER_CA, ORG_CONFIG
)


def _build_env(caller_org: str) -> dict:
    """构建peer命令所需的环境变量，以caller_org身份执行。"""
    cfg = ORG_CONFIG[caller_org]
    env = os.environ.copy()
    env["PATH"]                         = FAB_BIN + ":" + env.get("PATH", "")
    env["FABRIC_CFG_PATH"]              = CFG_PATH
    env["CORE_PEER_TLS_ENABLED"]        = "true"
    env["CORE_PEER_LOCALMSPID"]         = cfg["msp_id"]
    env["CORE_PEER_ADDRESS"]            = cfg["peer_address"]
    env["CORE_PEER_MSPCONFIGPATH"]      = cfg["msp_path"]
    env["CORE_PEER_TLS_ROOTCERT_FILE"]  = cfg["tls_cert"]
    env["FABRIC_LOGGING_SPEC"]          = "error"   # 抑制INFO日志污染stdout
    return env


def _endorser_flags(endorsing_orgs: list) -> list:
    """生成 --peerAddresses / --tlsRootCertFiles 参数列表。"""
    flags = []
    for org in endorsing_orgs:
        cfg = ORG_CONFIG[org]
        flags += ["--peerAddresses",    cfg["peer_address"]]
        flags += ["--tlsRootCertFiles", cfg["tls_cert"]]
    return flags


def invoke(
    caller_org: str,
    endorsing_orgs: list,
    contract: str,
    function: str,
    args: list,
    wait_seconds: float = 0.0,
) -> dict:
    """
    执行写交易（invoke）。
    返回 {"success": True} 或抛出 RuntimeError。

    参数：
        caller_org      调用身份（决定GetMSPID）
        endorsing_orgs  背书组织列表（至少2个Park）
        contract        合约名，如 ADMMCoordinationContract
        function        函数名，如 PublishNetworkParams
        args            参数列表（均为字符串）
    """
    func_name = f"{contract}:{function}"
    payload   = json.dumps({"function": func_name, "Args": [str(a) for a in args]})

    cmd = [
        "peer", "chaincode", "invoke",
        "-o",    "localhost:7050",
        "--ordererTLSHostnameOverride", "orderer.example.com",
        "-C",    CHANNEL_NAME,
        "-n",    CHAINCODE_NAME,
        "--tls",
        "--cafile", ORDERER_CA,
        "-c",    payload,
    ] + _endorser_flags(endorsing_orgs)

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_build_env(caller_org),
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"invoke {func_name} failed\n"
            f"stdout: {result.stdout.strip()}\n"
            f"stderr: {result.stderr.strip()}"
        )
    if wait_seconds > 0:
        time.sleep(wait_seconds)
    return {"success": True, "stderr": result.stderr.strip()}


def retry(action, attempts: int = 3, delay_seconds: float = 2.0):
    """
    对可能受提交可见性影响的操作做短重试。
    只用于阶段5A客户端侧，不改变链码语义。
    """
    last_error = None
    for i in range(attempts):
        try:
            return action()
        except RuntimeError as e:
            last_error = e
            if i == attempts - 1:
                break
            time.sleep(delay_seconds)
    raise last_error


def query(
    caller_org: str,
    contract: str,
    function: str,
    args: list,
) -> dict:
    """
    执行只读查询（query）。
    返回解析后的JSON dict，或抛出 RuntimeError。
    查询不需要背书Peer列表，直接发给caller_org的Peer。
    """
    func_name = f"{contract}:{function}"
    payload   = json.dumps({"function": func_name, "Args": [str(a) for a in args]})

    cmd = [
        "peer", "chaincode", "query",
        "-C", CHANNEL_NAME,
        "-n", CHAINCODE_NAME,
        "-c", payload,
    ]

    result = subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_build_env(caller_org),
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(
            f"query {func_name} failed\n"
            f"stdout: {result.stdout.strip()}\n"
            f"stderr: {result.stderr.strip()}"
        )

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"query {func_name} returned non-JSON:\n{result.stdout.strip()}"
        ) from e


def query_with_retry(
    caller_org: str,
    contract: str,
    function: str,
    args: list,
    attempts: int = 5,
    delay_seconds: float = 1.0,
) -> dict:
    """查询提交后的状态，允许账本可见性存在短暂延迟。"""
    return retry(
        lambda: query(caller_org, contract, function, args),
        attempts=attempts,
        delay_seconds=delay_seconds,
    )


def wait_until(description: str, predicate, attempts: int = 10, delay_seconds: float = 1.0):
    """
    等待某个链上状态满足条件。
    predicate需要返回 (ok, value)，ok=True时返回value。
    """
    last_value = None
    for _ in range(attempts):
        ok, value = predicate()
        last_value = value
        if ok:
            return value
        time.sleep(delay_seconds)
    raise RuntimeError(f"timeout waiting for {description}; last value: {last_value}")
