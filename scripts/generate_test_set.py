"""QA 测试集生成：DeepSeek API 基于语料生成初稿 → 人工 review 后固化。

用法: DEEPSEEK_API_KEY=sk-xxx .venv/bin/python scripts/generate_test_set.py
输出: assets/testsets/test_set.json（LLM 初稿）；人工修正后覆盖同名文件
"""
import asyncio
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx

API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
BASE_URL = "https://api.deepseek.com/v1"
MODEL = "deepseek-chat"

# 语料 ground truth 摘要（人工编写，基于 assets/corpus 10 文档的实际内容）
CORPUS_SUMMARY = {
    "handbook": """员工手册 v1.1（当前生效）:
- 年假: 司龄 1-3 年 10 天 / 3-5 年 12 天 / 5 年以上 15 天；当年 12 月 31 日前用完
- 病假: 需二甲以上医院证明；5 天内按日工资 80%，超出按当地最低工资
- 加班费: 工作日 1.5 倍 / 休息日 2 倍 / 节假日 3 倍
- 工资: 基本工资每月 5 日发放 + 季度绩效奖金（A/B/C 三档）
- 五险一金依法缴纳；差旅报销 30 日内提交
- 信息安全: 不得泄露内部资料，离职归还资产
- 注意: 员工手册 v1.0 的年假是 1-3 年 5 天（已废止，不应作为答案）
- HR 通知（扫描件）: 年假申请须提前 5 个工作日通过 OA 提交""",
    "compliance": """合规指南（中英文版内容一致）:
- 审计: 每年一次全面合规审计（外部机构）；重大变更 30 日内专项审计
- 审计范围: 财务/数据安全/采购/员工行为；审计报告留存 10 年
- 数据保留: 客户交易 5 年 / 财务凭证 10 年 / 员工档案离职后 3 年 / 访问日志 1 年
- 数据销毁: 超期按流程销毁，记录留 3 年
- 认证: ISO 27001，有效期 3 年，每年监督审核
- 隐私: 遵守《个人信息保护法》，泄露 24 小时内上报""",
    "technical": """API 规范 v3.2:
- 认证: Bearer token，24h 过期，refresh token 30 天
- 限流: 默认 1000 请求/分钟/key；突发 1500/分钟（最多 60s）；超限 429 + Retry-After
- 错误码: 400/401/429/500/503
- 端点: GET /v3/users/{id}（users:read）/ POST /v3/users（users:write）/ GET /v3/health（公开）

IT 安全策略 v1.4:
- 密码: ≥12 位含大小写数字符号，90 天过期，生产系统强制 MFA
- 远程访问: VPN + MFA，8 小时无活动超时；个人设备须入设备管理
- 事件上报: 发现 1 小时内报安全团队（security@example.com）
- 注意: 遗留手册 v2022 的"密码仅 8 位"已废止，不应作为答案""",
    "architecture": """架构总览 v2.1:
- 微服务: 12 个服务，API 网关统一出口，gRPC 服务间通信，Kafka 异步
- 部署: 云平台 3 可用区，每服务 ≥3 副本；数据库主从；Redis 集群 6 节点 3主3从
- 数据库选型: 用户/订单=MySQL、日志=ClickHouse、搜索=Elasticsearch
- 容量: 峰值 QPS 50000；CPU 80% 上限；连续 5 分钟 >70% 自动扩容 2 副本

应急响应 v3.0:
- 分级: SEV-1（全面中断/数据泄露）15 分钟响应 / SEV-2 30 分钟 / SEV-3 4 小时
- 升级链: 值班工程师→组长→经理→CTO，每级 +15 分钟
- 复盘: SEV-1/2 须 5 个工作日内复盘并产出带责任人和期限的行动项""",
}

GENERATE_PROMPT = """你是一个企业知识库的 QA 测试集标注员。基于下面给定的知识库内容摘要，生成高质量的问答测试集。

要求：
1. 生成 {n} 条问答对，覆盖全部 {n_domains} 个领域（handbook 员工手册 / compliance 合规 / technical 技术 / architecture 架构），每个领域至少 8 条
2. 其中 {n_zh} 条中文问题、{n_en} 条英文问题
3. 问题必须能用知识库内容回答（禁止编造知识库没有的事实）
4. ground_truth 必须是简洁的事实性答案，直接来自摘要内容
5. 输出 JSON 数组，每项格式：
   {{"question": "问题", "ground_truth": "标准答案", "domain": "handbook|compliance|technical|architecture", "language": "zh|en", "is_out_of_scope": false}}
6. 只输出 JSON，不要任何其他文本

知识库内容摘要：
{corpus}
"""


async def generate_batch(client, domain, n, n_zh, n_en):
    prompt = GENERATE_PROMPT.format(
        n=n, n_domains=1, n_zh=n_zh, n_en=n_en,
        corpus=CORPUS_SUMMARY[domain])
    resp = await client.post(
        f"{BASE_URL}/chat/completions",
        headers={"Authorization": f"Bearer {API_KEY}"},
        json={
            "model": MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": 4000,
        })
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    # 剥离可能的 markdown 代码块包装
    content = content.strip()
    if content.startswith("```"):
        content = content.split("\n", 1)[1]
        if content.endswith("```"):
            content = content[:-3]
    return json.loads(content)


def add_out_of_scope(qa_list):
    """混入 20% out-of-scope 问题（覆盖知识库外的常见干扰问题）"""
    oos = [
        {"question": "今天北京天气怎么样？", "ground_truth": "", "domain": "general",
         "language": "zh", "is_out_of_scope": True},
        {"question": "帮我推荐一部好看的电影", "ground_truth": "", "domain": "general",
         "language": "zh", "is_out_of_scope": True},
        {"question": "如何做红烧肉？", "ground_truth": "", "domain": "general",
         "language": "zh", "is_out_of_scope": True},
        {"question": "比特币现在价格多少？", "ground_truth": "", "domain": "general",
         "language": "zh", "is_out_of_scope": True},
        {"question": "Who won the World Cup in 2022?", "ground_truth": "", "domain": "general",
         "language": "en", "is_out_of_scope": True},
        {"question": "How to cook pasta?", "ground_truth": "", "domain": "general",
         "language": "en", "is_out_of_scope": True},
        {"question": "给我写一首关于春天的诗", "ground_truth": "", "domain": "general",
         "language": "zh", "is_out_of_scope": True},
        {"question": "明天的股市走势如何？", "ground_truth": "", "domain": "general",
         "language": "zh", "is_out_of_scope": True},
        {"question": "What is the best restaurant in Paris?", "ground_truth": "",
         "domain": "general", "language": "en", "is_out_of_scope": True},
        {"question": "帮我算一下 12345 乘以 6789", "ground_truth": "", "domain": "general",
         "language": "zh", "is_out_of_scope": True},
    ]
    # 40 in-scope + 10 out-of-scope = 50 条，OOS 占比 20%
    return qa_list + oos[: max(0, len(qa_list) // 4)]


async def main():
    if not API_KEY:
        print("错误: 缺少 DEEPSEEK_API_KEY 环境变量")
        sys.exit(1)
    async with httpx.AsyncClient(timeout=120) as client:
        all_qa = []
        # 每领域 10 条（8 中 2 英）
        for domain in ["handbook", "compliance", "technical", "architecture"]:
            try:
                batch = await generate_batch(client, domain, n=10, n_zh=8, n_en=2)
                all_qa.extend(batch)
                print(f"{domain}: {len(batch)} 条生成")
            except Exception as e:
                print(f"{domain}: 生成失败 {e}")

    qa = add_out_of_scope(all_qa)
    out_path = "assets/testsets/test_set.json"
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(qa, f, ensure_ascii=False, indent=2)
    test_hash = hashlib.md5(
        json.dumps(sorted(q["question"] for q in qa)).encode()).hexdigest()
    print(f"共 {len(qa)} 条 → {out_path}")
    print(f"test_set_hash: {test_hash}")


if __name__ == "__main__":
    asyncio.run(main())
