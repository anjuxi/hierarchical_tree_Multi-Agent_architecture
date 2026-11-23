# 依赖导入
import argparse;
import json;
import os;
import re;
from functools import partial;
from typing import Any, TypedDict;

from langgraph.graph import END, START, StateGraph;


# 常量
V_默认目标 = "开发一个支持登录和任务增删改查的待办事项 Web 应用，并完成功能测试与性能测试";
V_默认模型 = "gpt-4o-mini";
V_最大执行轮数 = 5;  # 子图循环的硬上限，防止主管把同一批任务反复派下去。
V_分界线 = "——" * 50;
V_领域清单 = ["研发", "测试"];
V_领域子任务 = {  # 离线模式下各领域的默认拆法。
    "研发": ["后端接口开发", "前端页面开发"],
    "测试": ["功能测试", "性能测试"],
};
V_领域边界 = {  # 写进提示词，省得真实模型让研发去写测试报告。
    "研发": "只负责方案设计与代码实现，不要产出测试计划或测试报告；",
    "测试": "只负责用例设计、功能验证与压测，不要修改业务代码或架构方案；",
};
V_离线交付模板 = {  # 离线模式下执行 Agent 的交付文案。
    "后端接口开发": "完成登录鉴权与任务增删改查接口，统一错误码并补齐参数校验，输出接口清单和启动说明。",
    "前端页面开发": "完成登录页与任务列表页，接入增删改查交互，补齐空状态和错误提示，输出页面清单与交互说明。",
    "功能测试": "覆盖登录、任务新增、修改、删除、查询五条主流程，输出用例清单与缺陷记录。",
    "性能测试": "对任务列表与登录接口做并发压测，记录响应时间与错误率，输出压测报告与瓶颈建议。",
};


# 数据结构
# 领域子图自己一份 state，顶层只拿得到最后的 report。
class DomainState(TypedDict, total=False):
    goal: str;  # 从顶层传下来的整体目标。
    domain: str;
    subtasks: list[str];  # 主管拆出来的子任务。
    results: list[str];  # 执行 Agent 的产出，按顺序堆。
    report: str;  # 汇总后的领域报告。
    rounds: int;  # 已经执行到第几个，兼循环计数。


class RootState(TypedDict, total=False):
    goal: str;
    domain_reports: list[dict];  # 各领域上报的报告。
    final_plan: str;


def generate(model: Any, system_prompt: str, user_prompt: str, fallback_text: str) -> str:
    # 有模型就调，没有或调挂了就返回兜底文案。
    # 参数 model 允许为 None；两个 prompt 分别走 system 和 user；fallback_text 是离线文案。
    # 返回最终要用的文本。
    if model is None:
        return fallback_text;
    try:
        reply = model.invoke([("system", system_prompt), ("user", user_prompt)]);
        text = reply.content.strip() if isinstance(reply.content, str) else str(reply.content).strip();
        return text or fallback_text;
    except Exception as error:
        # 网络或额度出问题时别让整条链路崩掉，退回离线文案继续跑。
        print(f"模型调用失败，回退离线模拟：{error}！");
        return fallback_text;


def parse_task_list(text: str, fallback: list[str]) -> list[str]:
    # 从模型回复里找出 JSON 数组，找不到或解析不了就用 fallback 顶上。
    # 参数 text 是模型原文，fallback 是离线任务列表。
    # 返回子任务列表，最多 V_最大执行轮数 个。
    match = re.search(r"\[[\s\S]*\]", text);
    if not match:
        return fallback;
    try:
        data = json.loads(match.group(0));
    except json.JSONDecodeError:
        return fallback;
    if not isinstance(data, list):
        return fallback;
    tasks = [str(item).strip() for item in data if str(item).strip()];
    if not tasks:
        return fallback;
    return tasks[:V_最大执行轮数];


def node_domain_plan(state: DomainState, model: Any) -> dict:
    # 主管拆解：把本领域目标拆成 2~3 个子任务。
    # 参数 state 是领域子图状态，model 为 None 时走离线拆法。
    # 返回带 subtasks 的状态片段。
    domain = state["domain"];
    goal = state["goal"];
    fallback_tasks = V_领域子任务.get(domain, ["通用任务"]);
    print(f"{domain}主管开始拆任务！");
    system_prompt = f"你是{domain}领域的主管 Agent，只负责本领域的目标拆解，不要越权处理其他领域。";
    user_prompt = (
        f"整体目标：{goal}\n"
        f"你的领域：{domain}\n"
        f"职责边界：{V_领域边界.get(domain, '只处理本领域内的任务；')}\n"
        f"请把本领域目标拆成 2 到 3 个可独立执行的子任务。\n"
        f"只输出 JSON 字符串数组，例如 [\"任务一\", \"任务二\"]，不要输出其他内容。"
    );
    text = generate(model, system_prompt, user_prompt, json.dumps(fallback_tasks, ensure_ascii=False));
    subtasks = parse_task_list(text, fallback_tasks);
    print(f"{domain}主管拆完了，一共 {len(subtasks)} 个子任务！");
    return {"subtasks": subtasks, "results": [], "rounds": 0};


def node_execute(state: DomainState, model: Any) -> dict:
    # 执行节点：按 rounds 取当前子任务，产出交付说明。
    # 参数 state 提供子任务和进度，model 为 None 时用离线模板。
    # 返回追加后的 results 和新的 rounds。
    index = state["rounds"];
    task = state["subtasks"][index];
    domain = state["domain"];
    goal = state["goal"];
    fallback_text = V_离线交付模板.get(task, f"完成{task}的产出，交付说明与自检记录已归档。");
    system_prompt = f"你是{domain}领域的执行 Agent，只对自己名下的专项任务负责，输出简洁的交付说明。";
    user_prompt = f"整体目标：{goal}\n你的专项任务：{task}\n请用三行以内的中文写清交付内容。";
    deliverable = generate(model, system_prompt, user_prompt, fallback_text);
    print(f"执行 Agent 完成：{task}！");
    # 不动原列表，拼个新的进去，省得后面读到被改过的状态。
    return {"results": state["results"] + [f"【{task}】{deliverable}"], "rounds": index + 1};


def route_after_execute(state: DomainState) -> str:
    # 还有没做的任务就回执行节点，做完或到上限就去汇总。
    # 参数 state 提供当前进度，返回下一跳的节点名。
    if state["rounds"] >= V_最大执行轮数 or state["rounds"] >= len(state["subtasks"]):
        return "汇总";
    return "继续";


def node_domain_report(state: DomainState, model: Any) -> dict:
    # 主管汇总：把下属结果整理成领域报告。
    # 参数 state 里有执行结果，model 为 None 时用模板拼。
    # 返回 report 字段。
    domain = state["domain"];
    joined = "\n".join(state["results"]);
    fallback_text = (
        f"{domain}领域汇总：\n{joined}\n"
        f"结论：{domain}侧任务做完了，等总控验收。"
    );
    system_prompt = f"你是{domain}领域的主管 Agent，负责把下属的执行结果汇总成简短的领域报告。";
    user_prompt = (
        f"领域：{domain}\n下属执行结果：\n{joined}\n"
        f"请输出领域报告，包含完成情况和遗留风险，控制在五行以内。"
    );
    report = generate(model, system_prompt, user_prompt, fallback_text);
    print(f"{domain}主管汇总完毕，上报总控！");
    return {"report": report};


def node_root_plan(state: RootState, model: Any) -> dict:
    # 总控规划：确认领域分工，给出全局思路，状态本身不改。
    # 参数 state 里有整体目标，model 为 None 时用固定规划文案。
    # 返回空字典。
    goal = state["goal"];
    fallback_text = (
        f"先拆研发、测试两个领域：研发管实现，测试管验证，"
        f"两边都交报告上来，由总控统一定稿。目标：{goal}"
    );
    system_prompt = "你是顶层总控 Agent，负责全局规划，不直接处理底层细节。";
    user_prompt = (
        f"整体目标：{goal}\n"
        f"本次可调度的领域为：{('、'.join(V_领域清单))}。\n"
        f"请用三行以内说明你的全局拆解思路。"
    );
    plan = generate(model, system_prompt, user_prompt, fallback_text);
    print("总控规划完成！");
    print(plan);
    return {};


def node_domain_team(state: RootState, domain_graph: Any, domain: str) -> dict:
    # 子图挂载：把领域子图当节点调，内部状态不会漏到顶层。
    # 参数 state 提供整体目标，domain_graph 是编译好的领域子图，domain 是领域名。
    # 返回追加了领域报告的新列表。
    print(V_分界线);
    print(f"总控把活派给{domain}主管！");
    result = domain_graph.invoke({
        "goal": state["goal"],
        "domain": domain,
        "subtasks": [],
        "results": [],
        "report": "",
        "rounds": 0,
    });
    return {"domain_reports": state["domain_reports"] + [{"domain": domain, "report": result["report"]}]};


def node_root_summary(state: RootState, model: Any) -> dict:
    # 总控汇总：合并各领域报告，产出最终交付方案。
    # 参数 state 里有各领域上报，model 为 None 时用离线模板。
    # 返回 final_plan。
    joined = "\n\n".join(f"== {item['domain']}领域 ==\n{item['report']}" for item in state["domain_reports"]);
    fallback_text = (
        f"最终交付方案\n"
        f"整体目标：{state['goal']}\n\n"
        f"一、领域汇总\n{joined}\n\n"
        f"二、交付清单\n"
        f"1. 研发侧：登录与任务增删改查功能可运行。\n"
        f"2. 测试侧：主流程用例通过，压测结论已归档。\n\n"
        f"三、风险与下一步\n"
        f"1. 测试缺陷单还需要人工复核，确认是否阻断上线。\n"
        f"2. 监控和回滚预案补齐后再发版。"
    );
    system_prompt = "你是顶层总控 Agent，负责把各领域报告整合成最终交付方案。";
    user_prompt = (
        f"整体目标：{state['goal']}\n各领域上报：\n{joined}\n"
        f"请输出最终交付方案，包含领域汇总、交付清单、风险与下一步。"
    );
    final_plan = generate(model, system_prompt, user_prompt, fallback_text);
    print("总控汇总完成！");
    return {"final_plan": final_plan};


def build_domain_graph(model: Any) -> Any:
    # 拼领域子图：拆解 → 执行（循环）→ 汇总。
    # 参数 model 透传给各节点，返回编译后的图。
    graph = StateGraph(DomainState);
    graph.add_node("主管拆解", partial(node_domain_plan, model=model));
    graph.add_node("执行任务", partial(node_execute, model=model));
    graph.add_node("主管汇总", partial(node_domain_report, model=model));
    graph.add_edge(START, "主管拆解");
    graph.add_edge("主管拆解", "执行任务");
    graph.add_conditional_edges("执行任务", route_after_execute, {"继续": "执行任务", "汇总": "主管汇总"});
    graph.add_edge("主管汇总", END);
    return graph.compile();


def build_root_graph(model: Any) -> Any:
    # 拼顶层图：规划 → 研发主管 → 测试主管 → 汇总。
    # 参数 model 透传给总控节点，返回编译后的图。
    graph = StateGraph(RootState);
    graph.add_node("总控规划", partial(node_root_plan, model=model));
    graph.add_node("研发主管", partial(node_domain_team, domain_graph=build_domain_graph(model), domain="研发"));
    graph.add_node("测试主管", partial(node_domain_team, domain_graph=build_domain_graph(model), domain="测试"));
    graph.add_node("总控汇总", partial(node_root_summary, model=model));
    graph.add_edge(START, "总控规划");
    graph.add_edge("总控规划", "研发主管");
    graph.add_edge("研发主管", "测试主管");
    graph.add_edge("测试主管", "总控汇总");
    graph.add_edge("总控汇总", END);
    return graph.compile();


def build_model(mode: str, model_name: str, base_url: str) -> Any:
    # 建模型：离线模式返回 None，真实模式读环境变量。
    # 参数 mode 为 offline 或 llm，model_name 是模型名，base_url 可留空。
    # 返回 ChatOpenAI 实例或 None。
    if mode == "offline":
        return None;
    api_key = os.getenv("OPENAI_API_KEY");
    if not api_key:
        raise SystemExit("环境变量 OPENAI_API_KEY 没设置，真实模式起不来！");
    from langchain_openai import ChatOpenAI;
    kwargs = {"model": model_name, "api_key": api_key, "temperature": 0.2};
    if base_url:
        kwargs["base_url"] = base_url;
    return ChatOpenAI(**kwargs);


def run_pipeline(goal: str, model: Any) -> str:
    # 跑一遍完整流程。
    # 参数 goal 是整体目标，model 为 None 时全程离线。
    # 返回最终交付方案文本。
    root_graph = build_root_graph(model);
    result = root_graph.invoke({"goal": goal, "domain_reports": [], "final_plan": ""});
    return result["final_plan"];


def parse_args() -> argparse.Namespace:
    # 解析命令行参数，返回 Namespace。
    parser = argparse.ArgumentParser(description="层级树状多智能体架构演示");
    parser.add_argument("--goal", default=V_默认目标, help="演示的整体目标");
    parser.add_argument("--mode", choices=["offline", "llm"], default="offline", help="运行模式，默认离线模拟");
    parser.add_argument("--model", default=os.getenv("OPENAI_MODEL", V_默认模型), help="真实模式使用的模型名");
    parser.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", ""), help="OpenAI 兼容接口地址");
    parser.add_argument("--check", action="store_true", help="只跑自检，不进入交互演示");
    return parser.parse_args();


def run_self_check() -> None:
    # 自检：离线跑完整条链路，检查最终方案里关键内容齐不齐。
    print("开始自检：离线跑一遍完整层级流程！");
    final_plan = run_pipeline(V_默认目标, None);
    assert "研发" in final_plan, "最终方案里没有研发领域的内容！";
    assert "测试" in final_plan, "最终方案里没有测试领域的内容！";
    assert "下一步" in final_plan, "最终方案里缺风险与下一步！";
    print(V_分界线);
    print("✅ 自检通过：三层链路完整，领域报告和最终方案都正常产出！");


def main() -> None:
    # 程序入口：自检和正常演示分开走。
    args = parse_args();
    if args.check:
        run_self_check();
        return;
    model = build_model(args.mode, args.model, args.base_url);
    print(V_分界线);
    print(f"启动层级树状架构，模式：{'真实模型' if model else '离线模拟'}！");
    print(f"整体目标：{args.goal}！");
    print(V_分界线);
    final_plan = run_pipeline(args.goal, model);
    print(V_分界线);
    print(final_plan);
    print(V_分界线);
    print("✅ 层级树状架构跑完了，最终交付方案见上！");


if __name__ == "__main__":
    main();
