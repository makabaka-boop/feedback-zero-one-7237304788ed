# 离线采集补传 / 快照 / 变更续取后端

Python（标准库 HTTP + psycopg2）+ PostgreSQL。完整设计、接口与运行说明见
[`docs/README.md`](docs/README.md)。

快速开始（本机已具备本地 PostgreSQL，unix socket `/tmp:55432`）：

```bash
bash run.sh                     # 初始化并启动 HTTP 服务于 127.0.0.1:8080
python3 -m scripts.admin replay # 重启后从空态重放，还原快照与版本
```

测试（共 995 项检查）：

```bash
python3 -m scripts.admin reset
python3 tests/test_spec.py          # 规格规则
python3 tests/test_edge_cases.py    # 边界与整批原子性
python3 tests/differential_test.py  # 独立祖先枚举对拍（Warshall oracle）
python3 tests/test_replay.py        # 重放还原
```
