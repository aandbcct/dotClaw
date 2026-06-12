## 新phase开发计划细节规划梳理
@skill:Self-Improving Agent @skill:brainstorming @skill:Planning with files @file:docs\architecture-and-roadmap.md 你先回忆一下phase3的任务， 现在进行phase3的开发规划，本次开发是长期建设，需要长期可维护，本次规划的目的是敲定phase3阶段的开发目的、开发方案、验收标准。需要将讨论沉淀一份phase3的详细开发文档，需要确定各个模块之间的层级关系和依赖关系，不需要输出具体代码。

## 开发计划完成，审计员审计开发计划
现在phase3的开发计划已经规划完成了，xx.md，你审计一下开发计划的可行性、长期发展性及有无漏洞，有漏洞或改进意见就提出意见，将审计意见放到phase4-roadmap-review.md

## 开发计划人员查看审计，并给出回执
开发计划审计员审计了你的 xx.md，并给出了审计建议 xx.md，请你查阅，根据审计情况，你判断是否同意审计员的审计要点，审计报告中的非事实内容可以不予理会，有问题的地方给出修改方案，不同意的地方给出理由，并同步修改开发计划。查看完审计文档后将审计文档状态修改为已查阅，并在文件中附上计划修正情况回执。

## 审计人员查看修正回执、修正后的开发文档，二次审计
开发计划人员已经完成了审计文档的查阅与修正回执，回执在 '审阅文档.md' 中，并且同步修正了 'phase3开发计划.md' ，请你查阅修正回执以及修正后的phase3开发计划，再次审计开发计划v2的可行性、长期发展性及有无漏洞，有漏洞或改进意见就提出意见，写入'审计文档2.md'中，如果没有问题，我就让开发人员进行phase3阶段的开发

## 生产计划完成，开发人员进行开发
@skill:Self-Improving Agent @skill:Ralph Loops @skill:Code Simplifier @skill:test-driven-development phase2阶段的开发规划已经完成，根据 开发文档.md 进行phase2阶段的开发，并将开发变更内容记录到phase5-record.md中，开头写变更日志表格，发现的问题不需要你写，参考 phase5-record.md

## 开发完成，测试人员根据实际开发情况审计项目
@skill:Code Reviewcode-review-prompt.mdphase5-record.md  目前phase5阶段开发已完成，根据要求，完成phase5阶段的code review，review结果写到phase5-codeReview.md中

## 代码审计文档给出，开发人员根据审计文档fix
@skill:Code Simplifier@skill:Ralph Loops 代码审查员完成了代码审查，根据审查报告 phase6-codeReview.md ，修复Critical、warming、minor。并更新变更日志 phase6-record.md 先和我讨论每个漏洞该怎么修复，进行任务拆分后保证每个漏洞都有确切的修复策略，再进行修复。修复完成后将修复情况记入phase6-codeReview.md 审查总览下面

## 开发完成更新开发文档与开发路线完成情况
根据项目中phase5实际开发实现情况，结合phase5-record.md，更新phase5-roadmap.md，更新 architecture-and-roadmap.md 中目标架构、当前实现状态、开发路线phase5部分内容，并在phase5部分后面附上架构变化，开发路线图部分不要修改其他阶段的内容，如果有本阶段设定的需要在后续阶段进行声明的内容，可以在后续phase的开发路线中补充。不要修改与 CowAgent 的对比分析、dotClaw 可借鉴的设计、不需要借鉴的部分。并同步更新README.md

## 更新层级架构图
参考 @file:docs\arch\memory-architecture.md，编写工具层架构图，放入arch文件夹