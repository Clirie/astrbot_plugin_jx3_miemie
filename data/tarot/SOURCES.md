# 塔罗资料来源

中文牌义、关键词、牌面象征和故事来自 [anois/tarot](https://github.com/anois/tarot)。对应源文件：`src/deck/data/card-meanings-zh.json` 与 `src/deck/data/card-lore.json`。该项目的许可证区分了程序代码与资料：程序代码采用 MIT License，Copyright (c) 2026 anois；`src/deck/data/` 牌义资料标注衍生自 [dariusk/corpora](https://github.com/dariusk/corpora/blob/master/data/divination/tarot_interpretations.json)（CC0）。完整原始许可说明保存在同目录 LICENSE-anois.txt。

本插件保留上述资料的内容，统一牌名及标点。运行时按实际抽中的牌和正逆位选择相应条目，不调用语言模型。

2026-09-27 的主题修订：对“求物、事业、学业”摘选原牌义中通用的条目，排除爱情、婚姻、伴侣等关系专用条目；原始牌义数据完整保留。主题通过明确前缀或有限关键词识别，多主题或无法归类的问题返回主题选项。这是本地主题检索，尚不具备自由中文语义理解、具体问题推理或牌阵综合解读能力。牌面故事仍作为图像背景说明保留，与主题牌义分区展示。

牌面采用 Pamela Colman Smith 绘制、1909 年出版的原始 Roses & Lilies 版本，来源 [Wikimedia Commons](https://commons.wikimedia.org/wiki/Category:Rider-Waite_tarot_deck_(Roses_%26_Lilies))，页面标注 Public domain。逐牌来源、许可证和 SHA-256 见 `art-sources.json`。

部分原始扫描文件通过 [henryperkins/tarot 的固定版本镜像](https://github.com/henryperkins/tarot/tree/cab0d262e397ad2dc2d25bb5fb08fe8934e97f8f/public/images/cards) 获取。以已下载的 Commons 愚人牌作字节级校验，两个来源的文件完全相同；各牌实际下载地址与镜像版本一并记录在 `art-sources.json` 中。

牌阵位置“处境、阻碍、建议”为阅读顺序，牌义作为娱乐与自省材料，不陈述已经发生或必将发生的事实。
