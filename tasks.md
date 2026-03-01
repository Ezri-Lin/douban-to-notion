# Notion 数据修复任务清单（2026-03-01）

- [x] Movie：回填可自动修复的 `MissingIMDBRating`
- [x] Movie：修复 `BrokenCover`（修复任务不更新 `CoverCheckedAt`）
- [x] Movie：修复完成后即时删除已修复问题对应的 `DataIssue` 标签（不等待审计）
- [x] Movie：处理 `ForeignActorChinese`（重刷为 IMDB 演员关系并即时清标签）
- [x] Movie：接入 HTML 兜底抓取（参考 notion_sync_data），并增加 m.douban 兜底（反爬场景下提取评分/封面）
- [ ] Movie：继续处理剩余高优先级问题（`MissingIMDB`、`MissingDirector`、`MissingIMDBRating`、`MissingDoubanRating`）
- [ ] 下一阶段：审计 Actor/Director 数据准确性
