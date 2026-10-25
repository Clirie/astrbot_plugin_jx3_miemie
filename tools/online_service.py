#!/usr/bin/env python3
"""Legacy online-service draft, disabled after capability verification.

The old file used an unspecified endpoint and signing secret. Its placeholder
response parsing could classify an absent gameLogin field as offline. There is
no verified provider contract here, so this file must not start a service or
claim to implement online-state queries. The plugin can still use an explicitly
configured third-party provider through its strict core.role_status parser.
"""


def query_online(params):
    return {
        "code": -1,
        "msg": "角色在线服务尚未实现：推栏直连接口未经验证，无法判断在线状态。",
    }


def main():
    raise SystemExit("在线服务草稿已停用：尚无经过验证的推栏直连实现。")


if __name__ == "__main__":
    main()
