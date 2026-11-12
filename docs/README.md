# 离线采集补传 / 快照 / 变更续取服务

纯 Python（标准库 HTTP 服务 + psycopg2）+ PostgreSQL 实现。解决离线采集终端
**乱序补传**问题：父事件可能晚到，事件先暂存（pending），待其全部祖先补齐后
级联激活；所有写入按因果关系取极大元形成实体快照，并以单调版本发布变更流。

## 目录结构

```
app/
  schema.sql      表结构（事件、占位归属、快照、版本、变更、原始批次）
  validation.py   纯函数批级校验，任一非法即整批拒绝
  engine.py       事务处理：归属固定、环检测、可见性定点、因果极大元、快照、版本
  oracle.py       独立祖先枚举（Warshall 传递闭包），与引擎算法不同，用于对拍
  db.py           连接与 schema 引导
  httpapi.py      HTTP 接口
scripts/admin.py  init-schema / replay / reset 命令
tests/            规格测试、随机差分对拍、重放还原测试
```

## 运行方式

```bash
# 1. 准备 PostgreSQL（本仓库在无 root 环境下用解压版 PG，unix socket /tmp:55432）
export APP_DSN='postgresql://app@/appdb?host=/tmp&port=55432'

# 2. 初始化
python -m scripts.admin init-schema

# 3. 启动 HTTP 服务（默认 127.0.0.1:8080，可用 APP_HOST/APP_PORT 覆盖）
python -m app.httpapi

# 重启后从空态重放、还原快照与版本
python -m scripts.admin replay
```

## HTTP 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/events` | 补传一批事件；接受 JSON 数组或 `{"events":[...]}` |
| GET | `/snapshot` | 当前完整快照（按 entity 排序） |
| GET | `/snapshot/{entity}` | 单实体；未知/仅待定实体返回空候选且不发布 |
| GET | `/changes?cursor=N` | 版本**严格大于** N 的整批变更，按版本升序 |

### 事件结构

```json
{"id":"p","entity":"E","parents":[],"op":"patch","set":{},"unset":[]}
```

- 标识（id / entity / parent）：非空 ASCII 字母数字串；`parents` 无重复。
- `op` 仅 `patch` / `erase`。
- `patch` 必须带顶层 `set`（对象）与 `unset`（数组）；`erase` 两者都不得带。
- 字段固定 `a`、`b`；`set` 值仅字符串或 `null`；`unset` 无重复且与 `set` 键不交。
- 未知顶层键、未知数据字段、类型错误等一律**整批拒绝（HTTP 422）**，不改状态。

### 响应约定

- 成功：`{"version":n,"new_events":k,"changed_entities":[...],"changes":[...],
  "retransmitted":bool,"accepted":bool}`
- 拒绝：HTTP 422 `{"error":"<reason>","detail":"..."}`，reason 为稳定机器码。

## 关键语义（与规格逐条对应）

1. **归属固定**：id 首次以事件 id 或 parent 引用出现即固定实体；之后改换实体
   或跨实体 parent 引用，整批拒绝。未定义的 parent 作为占位节点保存归属。
2. **环拒绝**：在“库中已有边 + 本批所有事件边”的并集图上做 DFS 染色，含自环、
   以及经过占位节点的环，一律整批拒绝。
3. **幂等**：同 id 且规范化后 JSON 结构完全相同 → 接受但不加版本；同 id 异内容
   （含 op 不同、set/unset 不同）→ 拒绝。
4. **可见性定点**：事件已定义，且其每个 parent 都是已定义且可见的事件时才可见；
   否则 pending。新补传的父节点可级联激活整链条。
5. **写入语义**：`patch` 写存在性 `true`；`erase` 写 `false` 且不产生任何字段
   候选并语义上清空两字段；空 patch 合法。`unset`/`erase` 是**缺失**，`null`
   是**有值**（`{"present":true,"value":null}`）。
6. **因果极大元**：每实体分别对存在性、字段 a、字段 b，在可见写入中取没有候选
   后代的候选；候选保留来源 id；同值并发不合并（全部保留）；未写字段不覆盖
   祖先（祖先候选继续存在）。
7. **快照**：仅有可见事件的实体入快照；有删除候选仍输出 a、b 全部字段且不选
   单一胜者（所有极大候选并列输出）。字段固定顺序 a、b，候选按 id 字节序。
8. **版本与变更**：初始版本 0 且无实体；只有引入新事件的批次版本 +1，纯重传
   不加；按 entity 排序发布快照实际变化的实体及其**完整新快照**；允许变更批为
   空数组（版本仍递增并落库）；续取返回版本严格大于游标的整批。
9. **原子持久化与并发**：每个批次在单事务内持事务级咨询锁
   （`pg_advisory_xact_lock`）完成所有表读写，并发提交串行等价；读事务总看到
   完整已发布版本；拒绝即回滚，不留状态。
10. **重放**：原始接受批次存于 `accepted_batches`；`replay` 清空全部派生表后
    从空态按序重走同一 `submit` 代码路径，逐版本还原快照与变更流。

## 测试

```bash
python -m scripts.admin reset
python tests/test_spec.py          # 73 项规格规则
python tests/differential_test.py  # 40 个随机图 × 多维度 对拍独立 oracle
python tests/test_replay.py        # 重放还原 + 新连接重启模拟
```

对拍使用的 `app/oracle.py` 采用 Warshall 传递闭包与独立定点实现，**不导入
engine 的任何函数**，因此不是同义反复。
