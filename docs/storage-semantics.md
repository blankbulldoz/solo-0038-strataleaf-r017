# 数据与兼容边界

物理表包括 meta、collections、versions、commits、receipts、index_defs、index_values、pins、consumers、aggregate_view_defs、aggregate_view_cells、aggregate_view_members。versions 的主键为集合、键、修订；body 的 SQL NULL 表示删除，JSON null 只能作为文档字段。collections 保存当前模式；index_values 是当前 head 的投影。commits 保存完整的操作序列；receipts 保存不受历史回收影响的请求摘要与结果。幂等键不能被不同操作复用，回放不重新执行前置条件。

聚合视图定义在 aggregate_view_defs（集合、分组路径、可选度量路径、可选谓词、已应用修订），分组合计在 aggregate_view_cells，每个文档当前的贡献投影在 aggregate_view_members。成员投影是撤销旧贡献的依据：更新先按成员表撤销旧分组与度量再计入新值，删除与到期清理只撤销贡献。cells 的 count 与 numeric_count 精确维护；min/max 在移除等于当前边界的度量时从同组剩余成员重算；sum 对整数度量精确，对浮点度量是增量结果，与一次性扫描可能存在末位差异，rebuild 可重新建基。视图维护与文档提交同属一个 IMMEDIATE 事务，提交回滚则视图不变；幂等回执回放与副本重复范围不触发维护，因此重开数据库或重复处理提交不会让计数漂移。就绪视图的 applied_rev 始终等于所属数据库的 head；视图定义不随副本包传播，从备份初始化的副本会继续维护已存在的视图。

floor 是仍可读取的最早修订，不代表每条物理版本的最小修订；每个键在 floor 之前的最后版本可能是构造 floor 读视图的基准，必须保留。commits 仅需保存大于 floor 的提交，消费者从自己的 ack 之后读取。pin 与消费者位置不能低于现存 floor。

模式包含 fields、required、additional，支持点路径类型；additional=false 限制顶层未知根字段，不递归禁止已声明对象中的子字段。字段不允许 null，除非该字段未声明类型；缺失可通过 required 控制。integer 排除 bool，number 接受整数及有限浮点。JSON 对象不允许非字符串键；浮点必须有限。索引忽略缺失字段与对象/数组，空值作为可索引标量，数值 1 和 1.0 共用索引值，true 与 1 不共用。

游标携带数据库身份、谓词摘要、读版本和最后键，并由数据库私有密钥签名。它不是权限凭据，不冻结模式，也不是保留租约。需要稳定的长时间读取，调用者自行管理具名保留点。

副本包是内部可信通道数据，校验和只用于检测传输损坏。正常备份恢复新主库保留逻辑内容而更换身份；副本初始化保留上游身份。分段追赶未完成时暂不提供当前索引，避免最新唯一约束错误约束中间历史视图。目录 epoch 可在没有文档提交时前进，最终空提交包仍能传递新的模式与索引。
