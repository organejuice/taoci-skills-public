# 文案和证据输入

每位导师一个私有 UTF-8 `content.json`。必填：`advisor_id`（M 编号）、`direction`（不带标题）、`research_interest`（不带标题）、`identity_reviewed: true`、`claims_reviewed: true`、`sources`。科研兴趣须包含当次导师姓名。

每个来源包含 `kind`（`official` 或 `paper`）、`title`、`url`、`checked_at`（实际核查日期）、`supports`（支持哪些表述）。至少一个官方身份/方向来源。论文还需真实 `authors`、`affiliation_check`、`published`、`doi`、`notice_check`。日期区分在线发表与卷期时间；`notice_check` 写出更正/撤稿检查依据。

没有直接论文依据但官方方向可确认时，填写 `paper_gap_note` 说明检索范围与局限，文案仅谈已核实方向；身份或主方向不能确认则不构建。避免把 DOI、核查日志等写入正式文书正文。

示例结构中的字段说明不代表已完成审阅：

```json
{
  "advisor_id": "M002",
  "direction": "填写经核验的方向",
  "research_interest": "学生对示例导师老师已核验的研究方向感兴趣。使用时改为真实、经审阅的文案。",
  "identity_reviewed": false,
  "claims_reviewed": false,
  "paper_gap_note": "如存在证据缺口，在此写出实际检索范围与局限。",
  "sources": [
    {
      "kind": "official",
      "title": "替换为真实官方来源标题",
      "url": "https://example.invalid/advisor",
      "checked_at": "实际核查日期",
      "supports": "替换为该来源实际支持的身份和方向"
    }
  ]
}
```

示例故意保留 `false`，不能直接构建。执行者实际核查并审阅文案后再设为 `true`。脚本只核验结构、字段与对应关系，不能认证同名作者、判断来源支持程度或识别所有夸大表述。

输入、manifest 与验收回执均属于用户私有运行数据。manifest 会记录导师信息、源文件路径及哈希、修改前后文字、文案、来源和输出哈希，不应随公开 skill 再分享。
