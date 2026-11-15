# 多阶段访谈抽样与现场替补后的可用入样概率

这是供本地讨论使用的数据应用。当前版本把抽签台扩展为从抽样设计到现场处置贯通的工具：在访谈工时与稀少专业覆盖约束下搜索可行设计，按选定设计生成正式名单与同层备用顺序并说明每个人的入样机会，把缺席、拒访、不在抽样框登记为不同事件，替补按事先随机顺序启用且不再沿用原概率说明，最后沿原设计给出当前可估计的总体、点估计与不确定性（或说明为何给不出）。约束不可同时满足时只交还冲突与可选放松，不退化为便利抽样。

## 环境与启动

使用 Python 3.11.14，数值依赖锁定在 requirements.txt。Docker 基础镜像按内容摘要固定；构建阶段安装依赖，服务、检查和演示阶段不下载数据、依赖或模型。所有业务记录都是本地构造的课程与研究管理资料，不包含个人或真实实验信息。

```sh
docker build -t probability-sample-desk:local .
docker run --rm -p 8080:8080 probability-sample-desk:local
```

浏览器打开本机 8080 端口。复用已有 Python 环境时，先在准备阶段安装锁定依赖，再运行服务：

```sh
python3 -m pip install --only-binary=:all: -r requirements.txt
python3 -m app.service
```

浏览器回归使用锁定的 Playwright 1.56.0 与其配套 Chromium。镜像构建阶段预装浏览器和系统库，测试与服务运行不下载任何依赖，不依赖宿主机的 Chrome 或 Edge。直接使用 Python 环境时，也需在准备阶段执行 `python3 -m playwright install --with-deps chromium`。

APP_STUDY 可以指定同结构本地资料，APP_STATE_DIR 指定批注与抽签日志目录，默认是 .local。批注只保存人工文字与当时的表单选择；抽签批次、现场回执与替补记录持久化在同目录的 draws.json，供复查与追溯。删除该目录可回到干净本地状态，原始资料不变。

## 工作流

网页"多阶段抽样工作台"与 /api/action 的各操作一一对应：

1. 生成可行设计（plan）：输入访谈工时预算与稀少专业最低覆盖。可行时按精确设计方差排序给出候选（含最坏工时、保证覆盖、方差可估计性）；不可同时满足时给出冲突与放松选项（提高预算、降低覆盖、限定总体），不生成名单。
2. 抽签（draw_v2）：选定设计与种子后抽出正式名单（每人附一阶入样概率与入样链文字说明）和各班同专业层的备用顺序，批次留存。
3. 现场回执与替补（receipt / substitute）：四种现场状态分别登记；仅缺席或拒访可启用同层下一名备用，框外个体转入总体修正。每次处置都写回批次记录。
4. 估计（estimate）：沿原设计给出当前估计依据——估计总体（框外已剔除）、正式名单受访者的 HT 点估计与设计方差（不可估计时为 null 并说明）、替补调整视图（显式模型假设，无设计方差）、覆盖核对与完整事件链。
5. 历史（draws / draw_detail）：全部批次可复查。

## 构建与检查

```sh
python3 -m compileall -q app tests
python3 -m unittest discover -s tests -v
docker run --rm --network none probability-sample-desk:local python -m unittest discover -s tests -v
```

测试覆盖现行领域语义、HTTP 错误边界和批注重开，并在真实 Chromium 中遍历资料表、核对表单响应与实时 HTTP 结果一致、保存批注并刷新确认；多阶段工作流同样在真实 Chromium 中从可行设计走到估计并与 HTTP 结果逐项比对。浏览器缺失或脚本报错会令测试失败，不跳过检查。data/analysis-cases.json 是后续分析可使用的独立检查资料，docs/analysis-contract.md 说明其统计意义和适用限制；这些资料不要求每次输出某种结论方向。新增分析需要自行补上相应数值、业务与页面回归验证。

## 短时间实机查看

```sh
python3 -m app.demo
```

该入口启动真实本地 HTTP 服务，从健康检查到当前业务操作逐项打印完整响应，随后关闭服务。默认资料在九十秒内完成，不读取预存日志。网页交互也使用同一服务。当前入口展示现有产品；后续新增的分析应通过真实接口进入该流程并保持短时可运行，不伪造预期成功。正式运行前先完成镜像构建和依赖准备。

## 代码位置

app/design.py 定义多阶段设计搜索、一阶/二阶入样概率、抽签、现场事件与估计的贯通语义；app/domain.py 保留现行操作并分发新操作；app/drawlog.py 持久化抽签批次与现场事件；app/service.py 是网页和接口共同使用的服务；web/app.js 提交表单并呈现记录与估计链；app/storage.py 保存工作批注。外部接口与分析边界分别见 docs/http-contract.md 和 docs/analysis-contract.md。
