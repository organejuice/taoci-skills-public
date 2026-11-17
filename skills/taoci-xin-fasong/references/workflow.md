# 材料与可选显示审阅

## 输入材料约定

本skill复用相邻 `taoci-yu-ziwo-chenshu/scripts/workflow_io.py` 读取工作簿名录。保留两个skill的相邻目录，或将roster_helper配置到使用者审核过的副本。

每个导师以Mxxx唯一匹配，数字部分作为文件编号。严格读取：

- `编号-套磁信-导师姓名.docx`
- `编号-自我陈述-导师姓名.docx`
- `自我陈述-导师姓名.pdf`

同名导师附件必须为 `自我陈述-导师姓名-学校.pdf`，并在attachment_overrides按M编号指定。不会模糊搜索、猜邮箱或回退到旧文件。材料skill生成的PDF带编号，需审核后复制为上述发送附件名并保留原件；复制内容必须相同。

套磁信首段 `邮件标题：<expected_subject>`；有且仅有一个 `导师姓名老师您好！`；正文结束于唯一电话落款，之后不能再有非空备注。subject最后 `-` 后的姓名要出现在“学生”落款中。当前电话解析适配中国大陆移动号码格式；其他地区格式需修改并验证解析器。

自我陈述须有一个“科研兴趣”段落，提及所选导师，与套磁信方向一致。DOCX与PDF提取文本一致，页数等于statement_pdf_pages。文本一致不能证明图片、字体或分页显示合格；先完成材料skill的视觉QA。

正文格式从源DOCX转换。含未支持的对象、修订、表格、链接或字段时停止，先在私有材料中处理，不静默丢弃。

## 可选client模式

protocol模式仅验证源件、格式参数、MIME及副本，不声称真实客户端显示通过。用户要求实际外观检查或报告显示异常时，使用已有的客户端及合适UI工具查看邮件。查看不是授权发送。

每个bundle的presentation-review.json应包含：advisor、bundle_sha256、eml_sha256、reviewed_by、reviewed_at、client、evidence、checks。reviewed_at为真实观察时间，ISO8601且带时区。

checks中的以下项目必须由实际观察者确认，而不是由脚本推导为true：body_visible、greeting_direction_correct、formatting_correct、recipient_correct、subject_correct、attachment_name_correct、attachment_content_correct。

client模式示例：

```powershell
python -X utf8 -B $mail --config $config send --review-mode client --bundles '../private-workspace/work/mail/batch-unique/M001/bundle.json' --reviews '../private-workspace/work/mail/batch-unique/M001/presentation-review.json' --authorization '../private-workspace/work/mail/batch-unique/authorization.json'
```

省略--reviews时读取每个bundle同目录记录；显式传入时按advisor匹配，须完整覆盖所选对象，不按参数次序配对。重新prepare、修改邮件或审阅记录后需重审。

自用测试草稿若作为显示证据，还需记录test_message_id、test_target及evidence_provenance，证明正文/HTML/PDF与正式包一致。替换成自用主题/To时不能声称已观察正式值；正式To与主题另按实际来源核对。不向导师发送测试邮件。
