# Third-party sources

The manuscript import adapter reuses InkOS's story-import Skill and reconstruction rubric verbatim,
and ports its TypeScript chapter splitter to Python. Upstream: https://github.com/Narcooo/inkos,
commit 8fc2ae57080b9821257dee3e37cc677e2b6f389a, licensed under AGPL-3.0.
The original files, license and checksums are preserved in src/pov_novel/vendor/inkos/.
Local changes include complete preamble preservation, source coordinates, natural file ordering,
JSON extraction, per-character memory isolation, recoverable import checkpoints and independent author files.
The combined project is distributed under the included AGPL-3.0 license.

Novel-OS review prompts remain under their original MIT license; see src/pov_novel/vendor/novel_os/.

InkOS 上下文管理扩展：固定同一提交，保留 semantic-input、context-compiler、
semantic-context-compiler、governed-context、governed-working-set、context-assembly、
context-compression、long-form-completion 和上游上下文压缩测试的原始快照。
Python 移植位于 semantic_context.py。保护当前任务、只编译可压缩历史、来源验证和
无损输入分块遵循上游方法；结构化 JSON 输出截断采用提高额度及拆小输入。
gateway.py 正文纯文本路径移植 long-form-completion 的确认截断续写和最大 4096 字符精确重叠合并。
故事增量、FTS5、技能和文件集合事务为本项目的 Python 实现，参照报告中的机制并适配现有 POV 隔离和归档提交点，
未声称将 InkOS/Novel-OS 全部运行平台或代码逐行移植。
逐文件原始路径与 SHA-256 见 vendor/inkos/provenance.json。
