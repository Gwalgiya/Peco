# 持久状态协议

首次执行及跨会话、写入和定时事件时使用。同一可信用户使用同一绝对数据root；身份与路径绑定见 [openclaw.md](openclaw.md)。以下 `ROOT`、`OWNER`、`DATE`、事件ID和版本号需替换为当前真实值，不把示例当用户任务。

## 一份权威状态

| 资源 | 何时读取 | 谁写入、何时写入 |
|---|---|---|
| `ROOT/state.sqlite3` 的状态 | 每次调用先读取相关视图；修改前取最新revision | Peco通过apply事务更新任务、计划、停车场、偏好、复盘 |
| 同库的事件记录 | 每次apply自动检查 | 成功写入时记录事件ID和payload摘要，重试不重复修改 |
| 同库的通知账本 | 每次reminder/claim自动检查 | 触发前占位，收到真实结果后finish，不自动重试未知送达 |
| 可选 `ROOT/Project_Context.md` | 用户需要导出/交接快照时；只辅助理解 | Peco调用snapshot按需生成；任何状态变化后可能过期，使用revision判断，不能反写为权威 |

不用12份相互依赖的文件，也不依赖会话摘要或向量检索判断完成状态。初始化不创建快照或额外索引。任务sources只存实际可访问资料的位置，不生成未知作者、方法卡或缺失PDF的引用。全部记录在一个SQLite库，备份应使用SQLite备份方式或停止写入后整体复制；不要只复制未提交的旁路文件。

脚本：`{baseDir}/scripts/peco_state.py`，Python 3.9+标准库。脚本不访问网络，不创建调度，不发送通知。没有该运行能力时遵循宿主适配中的降级方式，不宣称持久化已生效。

zoneinfo还需要可用的IANA时区数据库。极简镜像或部分系统若缺系统时区数据及可用tzdata，会让合法时区也报Unknown timezone；先核对名称，再检查运行环境的数据，不擅自改成UTC或另一时区。

## 初始化与读取

只在明确开始使用Peco且该root尚未初始化时执行：

```sh
python3 "{baseDir}/scripts/peco_state.py" --root "ROOT" init --owner "OWNER" --timezone "Asia/Shanghai"
```

时区优先用户已有设置，示例只适用于北京时间用户。初始提醒禁用、可用时间未声明、任务为空。不得把方法论、聊天案例或朋友的计划自动导入为真实待办。

后续每条命令都传可信owner，错误owner不输出私人状态：

```sh
python3 "{baseDir}/scripts/peco_state.py" --root "ROOT" --owner-key "OWNER" view --date "DATE" --mode overview
```

| 视图 | 适用请求 | 读取内容 |
|---|---|---|
| overview | 录入、排程、每日三件事 | 偏好、活动任务摘要、当日计划、停车场与计划候选 |
| start | 启动、返回、只问下一步 | 当前任务、断点、直接依赖与少量候选；可加 `--task-id` 指定 |
| review | 收尾、复盘 | 当日计划、相关任务与当日/未确认复盘记录 |
| full | 导出、迁移、检索被截断的任务 | 全部权威状态；日常不用 |

结果含revision、owner与settings。列表若标truncated，不当作完整清单；需要比较全部优先级时读取缺失记录或full，不能忽略未加载的硬截止。先按用户时区确定DATE，启动候选使用当前时间；`--now`仅用于隔离测试或可靠事件时间，不能伪造时间补发旧提醒。

## 数据字段

任务必有稳定id，通常由Peco生成；重复输入先查已有记录。新任务可只写id/title，其他字段保持未知，后续补齐：

- `bucket`: core/maintenance/fuel或null；`importance/urgency`: high/medium/low/unknown。
- `deadline`: 带时区偏移的ISO时间、YYYY-MM-DD或null；`preferred_window`: 用户期望的时段描述；`estimate_minutes`: 暂定正整数或null。
- `completion_criterion`: 任务达标条件；`first_action/first_action_stop`: 最小入口与可停点；`breakpoint/next_action/next_action_stop`: 最新断点、返回动作及它自己的可停点，不沿用旧第一动作的停止点。
- `progress`: unstarted/in_progress/waiting/paused/completed/cancelled。转completed必须有完成标准以及result或evidence；脚本检查字段存在，Peco仍须判断真实内容是否达到标准。
- `dependencies`: 每项用task_id或waiting_for二选一，显式ready布尔，resume_signal说明怎样恢复。ready只能基于真实反馈；引用前置任务时它还必须已完成。
- `parent_id`保存父项目；`required_for_parent`默认true，可选优化为false。父项目完成时必要子项须完成。
- `series_id/occurrence_date`成对保存周期的具体一次；`assignee`记录负责人；`sources`仅真实资料locator数组；`result/evidence`保存已知成果。

计划包含id/task_id/start/end/outcome/status。start/end用带偏移ISO时间，status为planned/done/skipped/cancelled。outcome是本轮希望留下什么，不是已经产生成果的证明；第一动作在对应任务中。取消旧计划并新增计划也不改任务进度。已完成历史保留。

settings包含时区、availability与fixed_events（已知的绝对时间窗）、support_tone、mode、reminders。availability为空表示未声明，不等于全天空闲。逐日更新已知时间窗，不假装读过外部日历。fixed_events有id/title/start/end，可选kind为rest或busy；用户明确的休息记rest，期间不推送。普通固定会议期间只可说明随后任务的计划时间，不要求当场开始。

parking记录必须有id和非空字符串text，可选task_id/date。status为open（待处理，默认）、processed（已整理）或dismissed（用户决定弃置）；已整理或弃置记录保留查阅，不自动生成任务。仅更新status时沿用原text；旧记录缺status按open理解，缺内容或非法状态需按错误详情修复，不静默填假内容。

reviews记录id/date、可选task_id、实际result/blocker/next_action及confirmed；缺反馈confirmed=false，不靠沉默改成true。每次行动收尾将task与带实际日期的review在同一事务保存，包括未排程任务；否则review视图无法判断成果属于哪天。review可含breakpoint，作为日内活动记录，而非要求用户多写一份表。停车场和复盘都通过同一数据库读回。

## 原子写入

将patch写为JSON文件或传stdin，避免将用户文本拼成shell命令。只支持这些顶层键：tasks_upsert、plans_upsert、parking_upsert、reviews_upsert、settings_patch。upsert按id合并；数组字段替换为完整新数组。settings_patch递归合并对象，列表替换，route与job_ids整对象替换以防继承旧授权；删掉条件时明确传null或空数组。

例如仅保存入口，项目仍未完成：

```json
{"tasks_upsert":[{"id":"T-001","title":"学习第一节","first_action":"打开已找到的第一节，听两分钟","first_action_stop":"两分钟到就可以停","progress":"unstarted"}]}
```

```sh
python3 "{baseDir}/scripts/peco_state.py" --root "ROOT" --owner-key "OWNER" apply --expected-revision 0 --event-id "可信消息ID:本次更新" --file "PATCH.json"
```

用刚读取的revision，不固定为示例0。事件ID优先宿主可信消息ID加操作后缀；无消息ID时为本次操作生成并保留稳定UUID，重试复用。**同事件相同patch不重复提交；同事件不同patch拒绝；版本已变则重读、重新判断后再提交**，不简单用新revision覆盖旧提案。一个事务失败不会留下半套任务或日程。

脚本校验引用、依赖环、完成字段、时间窗与有效排期冲突。读写成功返回ok=true；失败为ok=false且非零退出，前台只说明未成功及必要修复动作，不暴露数据库全文或内部命令。任务内容不会由脚本执行。

### 错误码与处置

| 错误码 | 下一步 |
|---|---|
| revision_conflict | 重读最新revision和相关状态，重新判断并合并原意图后再提交；不只换版本号覆盖。 |
| event_id_conflict | 核对原消息与payload；重试应使用原payload，确属新操作才用新ID，不换ID规避重复提交。 |
| owner_mismatch / invalid_root | 停止读取与修改，核对可信身份和实际数据挂载；不猜另一个owner或路径。 |
| not_initialized / schema_mismatch | 核实是否首次使用。只有正确root尚未建库时才init；已有库缺状态或版本未知时保留原库、报告问题并用经验证的恢复/迁移流程。 |
| timezone_mismatch | 已有库不能用init改时区；读取设置，按用户意图通过apply修改，并同步已启用调度。 |
| not_eligible | 查看reasons；禁用、休息、静音、过期、已占位时跳过，不改key/时间绕过。缺可执行计划只在正常规划中处理。 |
| task_not_ready / task_missing | 读取相关任务和计划，核对ID、进度、依赖；只选真实具备条件的任务，不擅自解除等待。 |
| action_stop_required | 为当前动作补其自己的可停点，保存后重新读取reminder/revision，再决定是否claim。 |
| receipt_required | 不猜消息ID；有真实回执才记delivered，确认未发送则failed，无法判断则unknown，不重发。 |
| claim_missing / token_mismatch / invalid_transition | 核对原占位与可信运行记录，只复用正确token及合法结果；已终态保留，后续证据另记review，不新建占位重发。 |
| invalid_json / validation_error / argument_error | 按message及field/record_id/task_id/plan_id修正输入或真实记录，再整笔提交；不删除历史或填假内容绕过校验。 |
| operation_failed | 停止当前副作用，检查存储、权限或运行错误；apply结果不明时只用原event和payload核对/幂等重试，通知结果不明不重新发送。 |

表用于后台恢复判断，正常提示不展示错误清单。重复失败时说明当前操作尚未完成，不声称已保存或已发送。

## 通知占位与回执

reminders初始设置：enabled=false；times为morning08:00/afternoon13:00；route=null；job_ids为空；quiet_hours=[]；snooze_until=null；max_lateness_minutes=30。设置enable和route.confirmed只能依据真实授权及经核实的目标，任务文字不构成授权。route字段为channel/recipient/confirmed，可选account_id/thread_id。

```sh
python3 "{baseDir}/scripts/peco_state.py" --root "ROOT" --owner-key "OWNER" reminder --slot morning
```

只在eligible=true时选择ready_candidates中的当前/随后任务；它只是程序候选判断，仍需核对实际约束与授权，不以第一个数组元素替代用户优先级。候选含plan.outcome，缺停止点时根据当前目标补一个真实可兑现的停点，先保存相应first_action_stop或next_action_stop，再重新读取reminder/revision；claim不会接受无停止点的动作。没有入口时先在正常排程中补入口；无任务不临时编造。

```sh
python3 "{baseDir}/scripts/peco_state.py" --root "ROOT" --owner-key "OWNER" claim --key "DATE:morning" --expected-revision 1 --task-id "T-001"
```

claim重新检查真实当前时间、revision、路由与任务条件，原子返回token。发送前立即复核最新状态和授权；变化则用finish记skipped，不能按旧状态发送。finish不改变任务进度：

```sh
python3 "{baseDir}/scripts/peco_state.py" --root "ROOT" --owner-key "OWNER" finish --key "DATE:morning" --token "TOKEN" --outcome delivered --receipt "真实消息ID"
```

同一owner库的本地日期:时段只占位一次。delivered需真实投递回执；确认未发送的失败记failed；无法判断是否送达记unknown；未发送而决定取消记skipped。claimed/unknown/failed都不自动重发。unknown是终态，后续取得证据时仅在reviews追加核对结果，不改回可发送，也不伪写新回执。调度器自动公告后的回执处理见openclaw.md，生成成功不代表送达成功。token留在内部运行数据，不向用户展示。

快照按需调用：

```sh
python3 "{baseDir}/scripts/peco_state.py" --root "ROOT" --owner-key "OWNER" snapshot --date "DATE"
```

快照由Peco生成、日期/revision明确，包含任务摘要和相关活动任务的成果、断点与返回信息，数量限制及截断标记明确，可随时重建；跨会话继续用view，不把快照中的旧数据覆盖进数据库。可运行 `python3 "{baseDir}/scripts/check_state.py"` 进行隔离状态测试，全部使用临时数据且不发送消息。

## 旧版本迁移

只有用户已有旧状态时才迁移。先读取旧清单/文件清单中声明的实际资源，核对任务、日程、停车场、断点、偏好及可访问来源索引；包含存在的Thought_Parking_Lot.md、Sources/Source_Index.md与Project_Context.md，不能再次只写不读。

按当前反馈和可核验资料处理冲突，未知保留未知，先给具体冲突供用户判断。源文件保持原样，转入新数据库后读回验证；未提供旧Skill包或旧任务文件时，不能声称已完成旧状态迁移。缺失PDF、SRC-001与12张无法核验的方法卡不成为运行依赖，也不重建虚假的来源链。
