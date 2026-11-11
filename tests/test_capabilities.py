"""能力接入测试：钩子库 / 打分选优 / 高光挑选 / BGM 让位 / 逐词字幕 / 分段切分 / 模板兜底标记。

零第三方依赖、不联网、不用 FFmpeg、不用 config.json（cfg 只在测试里用内联字典，
不当生产配置）。运行：python -m unittest discover -s tests -p "test_*.py" -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import clips, compose, highlights, hooks, llm  # noqa: E402
from core.script_score import score_script  # noqa: E402

# 测试专用内联 cfg（只活在测试进程里，不写盘、不当生产配置）
MIN_CFG = {
    "brand": {"name": "凌扬运动", "slogan": "科学训练",
              "cta": "来门店试一次", "handle": "@凌扬运动"},
    "niche": {"audience": "新手、上班族", "products": ["体态评估"],
              "tone": "口语化"},
    "video": {"max_scenes": 5},
    "compliance": {"local_banned": ["保证", "包瘦", "最有效", "100%"],
                   "soft_banned": ["必须"], "external_api": False},
    "llm": {"providers": []},
    "local": {},
}

TOPIC = {"title": "新手第一次进健身房该怎么练",
         "hook": "第一次进健身房，别急着加片",
         "angle": "新手入门", "kind": "evergreen", "hot_ref": ""}


def _script(hook_text: str, scenes: list[str]) -> dict:
    return {
        "title": "新手第一次进健身房该怎么练",
        "cover_text": "别急着加片",
        "caption": "新手入门 #健身",
        "scenes": [{"index": i + 1, "narration": t, "on_screen": "别急着加片",
                    "sub": "先走稳动作", "visual_prompt": "教练示范中景"}
                   for i, t in enumerate(scenes)],
    }


GOOD = _script("第一次进健身房，别急着加片", [
    "第一次进健身房，你是不是一上来就加片？先别急，3 个动作走稳再说。",
    "第一周别追重量，每个动作 2 组、每组 12 次，先把动作轨迹走稳。",
    "进馆先花 8 分钟热身，关节活动加动态拉伸，冷启动别上大重量。",
    "判断强度合不合适，看第二天：轻微酸胀正常，影响上下楼就减量。",
    "来门店试一次，教练先给你做一次体态评估。",
])

BAD = _script("大家好今天给大家分享健身", [
    "大家好今天给大家分享一些健身的心得体会希望大家能够喜欢并坚持下去保证一周就能看到非常明显的效果这是最有效的训练方法",
    "健身真的非常重要大家一定要坚持下去每天都要努力训练不要偷懒这样才能拥有好身材和健康的身体",
    "今天的内容就是这些希望大家多多点赞关注我们下期再见谢谢大家的支持与厚爱我们会继续努力更新更多内容",
    "健身需要长期坚持不能三天打鱼两天晒网要制定科学的计划并且严格执行下去才能看到效果",
    "好了今天就讲到这里大家记得点赞收藏关注我们",
])


class TestHooks(unittest.TestCase):
    def test_five_structures(self):
        ids = [s["id"] for s in hooks.list_structures()]
        self.assertEqual(ids, ["pain_question", "contrarian", "numbered_list",
                               "scene", "conclusion_first"])

    def test_pick_is_stable_per_topic(self):
        a = hooks.pick_structure(TOPIC)
        b = hooks.pick_structure(TOPIC)
        self.assertEqual(a, b)
        self.assertIn(a, [s["id"] for s in hooks.list_structures()])

    def test_pick_rotates_across_topics(self):
        picked = {hooks.pick_structure({"title": t}) for t in
                  ["新手深蹲膝盖疼怎么办", "减脂先记饮食还是先加练",
                   "圆肩驼背每天练什么", "蛋白粉到底要不要吃",
                   "一周练几次最合适", "热身拉伸怎么做才对"]}
        self.assertGreaterEqual(len(picked), 2)

    def test_config_can_pin_default(self):
        cfg = {"hooks": {"default": "scene"}}
        self.assertEqual(hooks.pick_structure(TOPIC, cfg), "scene")

    def test_structure_block_mentions_roles(self):
        block = hooks.structure_block("numbered_list")
        self.assertIn("数字", block)
        self.assertIn("分镜依次为", block)

    def test_empty_enabled_means_none(self):
        """GPT 第二次复核第 3 节：enabled 显式空列表 = 一个都不启用，
        未配（None）才是全启用，两者语义不再含糊。"""
        self.assertEqual(hooks.list_structures({"hooks": {"enabled": []}}), [])
        self.assertEqual(len(hooks.list_structures({"hooks": {}})), 5)
        self.assertEqual(len(hooks.list_structures(None)), 5)
        # 空启用列表时 pick_structure 回退到第一个结构，不崩。
        self.assertEqual(hooks.pick_structure(TOPIC, {"hooks": {"enabled": []}}),
                         "pain_question")

    def test_get_structure_returns_deep_copy(self):
        """GPT 第二次复核第 3 节：调用方改返回的 hook_examples 不得污染
        全局 STRUCTURES。"""
        s1 = hooks.get_structure("pain_question")
        s1["hook_examples"].append("调用方改的例子")
        s2 = hooks.get_structure("pain_question")
        self.assertNotIn("调用方改的例子", s2["hook_examples"])
        self.assertEqual(s2["hook_examples"], hooks._BY_ID["pain_question"]["hook_examples"])


class TestScore(unittest.TestCase):
    def test_good_beats_bad(self):
        self.assertGreater(score_script(GOOD)["total"],
                           score_script(BAD)["total"])

    def test_banned_word_costs_compliance_points(self):
        bad = _script("保证瘦", ["保证一周瘦下来，这是最有效的办法，每天练 3 组。"] * 5)
        self.assertLess(score_script(bad)["compliance"], 25)
        self.assertIn("保证", " ".join(score_script(bad)["issues"]))

    def test_long_sentence_costs_oral_points(self):
        long_one = _script("钩子", [
            "这句话特别特别长中间没有任何停顿一直写下去超过二十五个字都不带一个逗号所以念起来一定会喘不上气" * 2] * 5)
        self.assertLess(score_script(long_one)["oral"], 25)

    def test_empty_script_scores_zero_on_content(self):
        # 空脚本内容分（钩子/密度/口语）为 0；合规项无命中，按规则就是满分，不硬扣
        sc = score_script({"scenes": []})
        self.assertEqual(sc["hook"] + sc["density"] + sc["oral"], 0)
        self.assertEqual(sc["compliance"], 25)

    def test_visual_prompt_digits_do_not_count(self):
        """GPT 第二次复核第 5 节第 6 项：visual_prompt 里的数字
        （如“3 个机位”）不是口播信息，不得算进信息密度分。"""
        script = _script("钩子", ["练错了先停下来，看看动作轨迹对不对。"] * 5)
        for s in script["scenes"]:
            s["visual_prompt"] = "3 个机位轮切，特写 2 处器械"
        self.assertEqual(score_script(script)["density"], 0)
        # 对照：同样的数字放进口播就得分。
        script2 = _script("钩子", ["第一周练 3 组，每组 12 次。"] * 5)
        self.assertGreater(score_script(script2)["density"], 0)

    def test_banned_word_in_on_screen_or_sub_is_caught(self):
        """GPT 第二次复核第 5 节第 6 项：禁词藏在画面大字/小字里也必须
        被扫到，不能只扫口播和标题。"""
        script = _script("钩子", ["练错了先停下来，看看动作轨迹对不对。"] * 5)
        script["scenes"][0]["on_screen"] = "保证瘦"
        script["scenes"][1]["sub"] = "包瘦不反弹"
        sc = score_script(script)
        self.assertLess(sc["compliance"], 25)
        self.assertIn("保证", " ".join(sc["issues"]))
        self.assertIn("包瘦", " ".join(sc["issues"]))


class TestHighlights(unittest.TestCase):
    SEGS = [
        {"start": 0.0, "end": 3.0, "text": "大家好今天来聊聊健身"},
        {"start": 3.0, "end": 8.0, "text": "深蹲的时候膝盖为什么会疼？先看 2 个地方"},
        {"start": 8.0, "end": 12.0, "text": "第一个是膝盖对齐脚尖，别内扣"},
        {"start": 12.0, "end": 20.0, "text": "第二个是蹲到髋略低于膝就够了，不必追求深度"},
        {"start": 20.0, "end": 24.0, "text": "好了今天就到这里"},
    ]

    def test_picks_keyword_dense_window(self):
        best = highlights.pick_highlight(self.SEGS)
        self.assertIsNotNone(best)
        self.assertIn(1, best["segment_indexes"])
        self.assertIn("深蹲", best["text"])

    def test_window_within_bounds(self):
        best = highlights.pick_highlight(self.SEGS)
        self.assertGreaterEqual(best["end"] - best["start"], 8.0)
        self.assertLessEqual(best["end"] - best["start"], 30.0)

    def test_empty_returns_none(self):
        self.assertIsNone(highlights.pick_highlight([]))
        self.assertIsNone(highlights.pick_highlight(None))

    def test_unsorted_input_is_sorted(self):
        """GPT 第二次复核第 5 节第 5 项：乱序输入先按时间排序再挑窗口，
        不依赖调用方事先排好序。"""
        shuffled = list(reversed(self.SEGS))
        best = highlights.pick_highlight(shuffled)
        self.assertIsNotNone(best)
        self.assertIn("深蹲", best["text"])
        # 窗口 [0,1,2] 与 [1,2,3] 同分 12.0，先出现的胜出；下标按排序后
        # 列表计，乱序输入也能挑到同一窗口。
        self.assertEqual(best["segment_indexes"], [0, 1, 2])
        self.assertEqual((best["start"], best["end"]), (0.0, 12.0))

    def test_invalid_segments_are_dropped(self):
        """GPT 第二次复核第 5 节第 5 项：负起点、end 小于 start、非数字
        时间戳一律丢弃，不静默算出错误窗口；只剩无效段时返回 None。"""
        segs = [
            {"start": -2.0, "end": 3.0, "text": "负起点"},
            {"start": 5.0, "end": 4.0, "text": "end 小于 start"},
            {"start": "x", "end": 10.0, "text": "非数字"},
            {"start": 0.0, "end": 8.0, "text": "深蹲膝盖为什么会疼？先看 2 个地方"},
        ]
        best = highlights.pick_highlight(segs, min_dur=8.0)
        self.assertIsNotNone(best)
        self.assertNotIn("负起点", best["text"])
        self.assertNotIn("非数字", best["text"])
        self.assertEqual(best["start"], 0.0)
        self.assertEqual(best["end"], 8.0)
        bad_only = [{"start": -1.0, "end": 2.0, "text": "坏"},
                    {"start": 5.0, "end": 3.0, "text": "坏"}]
        self.assertIsNone(highlights.pick_highlight(bad_only))

    def test_large_gap_not_bridged(self):
        """GPT 第二次复核第 5 节第 5 项：隔 60 秒的两段话不能拼成一个
        连续窗口；窗口只能取单侧。"""
        segs = [
            {"start": 0.0, "end": 9.0, "text": "深蹲膝盖为什么会疼？先看 2 个地方"},
            {"start": 69.0, "end": 78.0, "text": "第二个是蹲到髋略低于膝就够了"},
        ]
        best = highlights.pick_highlight(segs)
        self.assertIsNotNone(best)
        self.assertLessEqual(best["end"] - best["start"], 30.0)
        self.assertTrue(best["start"] < 10.0 or best["start"] >= 69.0)
        self.assertNotIn("第二个是", best["text"] if best["start"] < 10.0
                         else "")

    def test_single_long_segment_clamped_to_max_dur(self):
        """GPT 第二次复核第 5 节第 5 项：单段回退也不得超过 max_dur。"""
        segs = [{"start": 0.0, "end": 90.0, "text": "深蹲膝盖为什么会疼？"}]
        best = highlights.pick_highlight(segs, min_dur=8.0, max_dur=30.0)
        self.assertIsNotNone(best)
        self.assertEqual(best["end"] - best["start"], 30.0)


class TestDucking(unittest.TestCase):
    def test_voice_quieter_than_gap(self):
        tl = [{"start": 0.0, "voice_dur": 4.0},
              {"start": 5.0, "voice_dur": 4.0}]
        env = compose._gain_envelope(tl, 10.0, 0.10, 0.03)
        self.assertLess(env[40], env[90])   # 2.0s 人声段 < 4.5s 留白段
        self.assertAlmostEqual(env[40], 0.03, places=2)
        self.assertAlmostEqual(env[90], 0.10, places=2)

    def test_envelope_covers_total(self):
        env = compose._gain_envelope([{"start": 0.0, "voice_dur": 2.0}],
                                     5.0, 0.10, 0.03)
        self.assertEqual(len(env), 101)


class TestWordSrt(unittest.TestCase):
    def test_clauses_cover_narration(self):
        script = {"scenes": [{"narration": "第一周别追重量，每个动作2组、每组12次，先把动作轨迹走稳。"}]}
        timeline = [{"start": 0.0, "voice_dur": 6.0}]
        srt = compose._build_srt(script, timeline)
        joined = "".join(l for l in srt.splitlines()
                         if l and not l[0].isdigit() and "-->" not in l)
        self.assertEqual(joined, "第一周别追重量每个动作2组每组12次先把动作轨迹走稳")

    def test_times_monotonic_within_scene(self):
        script = {"scenes": [{"narration": "第一周别追重量，每个动作2组、每组12次，先把动作轨迹走稳。"}]}
        timeline = [{"start": 2.0, "voice_dur": 6.0}]
        srt = compose._build_srt(script, timeline)
        spans = [l.split(" --> ") for l in srt.splitlines() if "-->" in l]
        self.assertGreater(len(spans), 1)
        self.assertTrue(all(s < e for s, e in spans))
        self.assertTrue(spans[0][0].startswith("00:00:02"))

    def test_real_word_timestamps_win(self):
        script = {"scenes": [{"narration": "你好世界"}]}
        timeline = [{"start": 0.0, "voice_dur": 4.0}]
        voices = [{"words": [{"text": "你好", "start": 0.5, "end": 1.5},
                             {"text": "世界", "start": 1.5, "end": 2.5}]}]
        srt = compose._build_srt(script, timeline, voices)
        self.assertIn("00:00:00,500 --> 00:00:01,500", srt)

    def test_real_word_timestamps_are_clamped(self):
        """GPT 第二次复核第 5 节第 3 项：真实词级时间戳夹到本镜区间内 ——
        负起点截到 0，超长结尾截到总时长，完全越界的词丢弃。"""
        words = [{"text": "你", "start": -1.0, "end": 0.5},
                 {"text": "好", "start": 3.5, "end": 9.0},
                 {"text": "世", "start": 12.0, "end": 15.0}]
        spans = compose._word_timings("你好世界", 0.0, 4.0, words)
        self.assertEqual(len(spans), 2)
        self.assertEqual(spans[0], (0.0, 0.5, "你"))
        self.assertEqual(spans[1], (3.5, 4.0, "好"))

    def test_overlapping_words_do_not_overlap(self):
        """GPT 第二次复核第 5 节第 3 项：与上一条重叠的词，起点推到
        上一条结尾；完全被上一条盖住的词丢弃。"""
        words = [{"text": "你", "start": 0.0, "end": 2.0},
                 {"text": "好", "start": 1.0, "end": 3.0},
                 {"text": "世", "start": 1.5, "end": 1.8}]
        spans = compose._word_timings("你好世界", 0.0, 4.0, words)
        self.assertEqual(spans[0], (0.0, 2.0, "你"))
        self.assertEqual(spans[1], (2.0, 3.0, "好"))
        self.assertEqual(len(spans), 2)

    def test_many_short_clauses_stay_in_range(self):
        """GPT 第二次复核第 5 节第 3 项：大量短句估算时，所有条目起止
        都在区间内、不重叠、末段结尾恒等于总时长。"""
        narration = "，".join(f"第{i}句" for i in range(1, 21))
        spans = compose._word_timings(narration, 2.0, 2.0)
        self.assertTrue(all(2.0 <= s < e <= 4.0 for s, e, _ in spans))
        self.assertTrue(all(spans[i][1] == spans[i + 1][0]
                            for i in range(len(spans) - 1)))
        self.assertEqual(spans[-1][1], 4.0)

    def test_distribute_sums_exactly(self):
        spans = compose._distribute(5.0, [1.0, 1.0, 1.0])
        self.assertEqual(spans[-1][1], 5.0)
        self.assertTrue(all(spans[i][1] == spans[i + 1][0]
                            for i in range(len(spans) - 1)))
        self.assertTrue(all(0.0 <= s < e <= 5.0 for s, e in spans))


class TestSplitSegments(unittest.TestCase):
    def test_two_to_three_parts_sum_to_duration(self):
        parts = clips.split_segments(12.0, "第一周别追重量，每个动作2组，每组12次，先把动作轨迹走稳。")
        self.assertIn(len(parts), (2, 3))
        self.assertAlmostEqual(sum(p["duration"] for p in parts), 12.0, places=1)
        self.assertTrue(all(p["duration"] >= 1.0 for p in parts))
        offsets = [p["start_offset"] for p in parts]
        self.assertEqual(offsets, sorted(offsets))

    def test_short_duration_two_parts(self):
        parts = clips.split_segments(5.0, "先热身，再上重量。")
        self.assertEqual(len(parts), 2)

    def test_no_narration_still_splits(self):
        parts = clips.split_segments(9.0)
        self.assertEqual(len(parts), 3)

    def test_too_short_duration_returns_single_part(self):
        """GPT 第二次复核第 3 节：总时长凑不够 2 段、每段至少 1 秒时，
        只返回一段（调用方回退整段渲染），不硬切出负时长或不足 1 秒的段。"""
        parts = clips.split_segments(1.5, "第一周别追重量，每个动作2组，每组12次。")
        self.assertEqual(len(parts), 1)
        self.assertEqual(parts[0]["start_offset"], 0.0)
        self.assertAlmostEqual(parts[0]["duration"], 1.5, places=2)

    def test_long_clause_never_produces_negative_duration(self):
        """GPT 第二次复核第 3 节：一句超长断句占满总时长时，各段仍
        ≥ 1 秒、之和恒等于总时长，不出现负时长。"""
        narration = "这" * 200 + "，短句。"
        parts = clips.split_segments(3.0, narration)
        self.assertTrue(all(p["duration"] >= 1.0 for p in parts))
        self.assertAlmostEqual(sum(p["duration"] for p in parts), 3.0, places=1)
        offsets = [p["start_offset"] for p in parts]
        self.assertEqual(offsets, sorted(offsets))


class TestFallbackMarking(unittest.TestCase):
    def test_no_provider_is_not_flagged_for_review(self):
        with mock.patch.object(llm, "available_providers", return_value=[]):
            out = llm.generate_scripts([TOPIC], MIN_CFG)
        self.assertEqual(out[0]["generator"], "template")
        self.assertEqual(out[0]["fallback"]["reason"], "no_provider")
        self.assertFalse(out[0]["fallback"]["needs_review"])

    def test_llm_failed_is_flagged_for_review(self):
        fake = [{"name": "gateway:magpie", "model": "x", "base_url": "",
                 "api_key": "", "key_env": ""}]
        with mock.patch.object(llm, "available_providers", return_value=fake), \
             mock.patch.object(llm, "_llm_script", return_value=None):
            out = llm.generate_scripts([TOPIC], MIN_CFG)
        self.assertEqual(out[0]["generator"], "template")
        self.assertEqual(out[0]["fallback"]["reason"], "llm_failed")
        self.assertTrue(out[0]["fallback"]["needs_review"])

    def test_variant_scoring_picks_best(self):
        fake = [{"name": "gateway:magpie", "model": "x", "base_url": "",
                 "api_key": "", "key_env": ""}]
        variants = [dict(BAD, generator="llm:x"), dict(GOOD, generator="llm:x")]
        with mock.patch.object(llm, "_llm_script", side_effect=variants):
            best, scored = llm.generate_script_variants(TOPIC, MIN_CFG, fake, count=2)
        self.assertIsNotNone(best)
        self.assertEqual(best["scenes"][0]["narration"],
                         GOOD["scenes"][0]["narration"])
        self.assertEqual(len(scored), 2)


class TestDeAiSmell(unittest.TestCase):
    """去 AI 味：痛点落具体场景、冒号不断句、合规不误伤第一次、套话扣分。"""

    def test_pain_is_topic_specific(self):
        a = llm.template_script(TOPIC, MIN_CFG)["scenes"][1]["narration"]
        b = llm.template_script(
            {"title": "减脂先记3天饮食再谈训练", "hook": "减脂先别加练",
             "angle": "减脂", "kind": "evergreen", "hot_ref": ""}, MIN_CFG)["scenes"][1]["narration"]
        self.assertNotEqual(a, b)
        self.assertNotIn("不是不努力", a + b)

    def test_first_clause_breaks_on_colon(self):
        self.assertEqual(llm._first_clause("进馆先花8分钟热身：关节活动加动态拉伸", 18),
                         "进馆先花8分钟热身")

    def test_compliance_does_not_flag_first_time(self):
        self.assertEqual(score_script(GOOD)["compliance"], 25)

    def test_cliche_penalty(self):
        cliche = _script("钩子", ["首先，让我们一起在快节奏的时代开启你的训练。"] * 5)
        self.assertLess(score_script(cliche)["oral"], score_script(GOOD)["oral"])


class TestHotHook(unittest.TestCase):
    """热点当话术切口：中性热点能挂开头，负面新闻挂不上，模板首镜讲完立刻回主题。"""

    def test_pick_hook_hots_keeps_stock_drops_crime(self):
        from core.topics import pick_hook_hots
        hot = {"top": [
            {"title": "A股三大指数集体高开", "platform": "抖音", "hot": 1, "rank": 1},
            {"title": "“花坛藏尸案”一审宣判", "platform": "抖音", "hot": 1, "rank": 2},
            {"title": "冷空气已致广东降温超10℃", "platform": "头条", "hot": 1, "rank": 3},
            {"title": "一个讨厌了很久的健身博主", "platform": "B站运动榜", "hot": 1, "rank": 4},
            {"title": "IG老板怒斥AL训练赛放鸽子", "platform": "贴吧", "hot": 1, "rank": 5},
            {"title": "乌方在三方会谈前表态", "platform": "抖音", "hot": 1, "rank": 6},
            {"title": "华为推出全新小折叠电脑", "platform": "抖音", "hot": 1, "rank": 7},
            {"title": "江苏姑娘赴河北找到三四块流星陨石", "platform": "抖音", "hot": 1, "rank": 8},
        ]}
        out = pick_hook_hots(hot, 6)
        titles = [o["title"] for o in out]
        self.assertIn("A股三大指数集体高开", titles)
        self.assertIn("冷空气已致广东降温超10℃", titles)
        self.assertNotIn("“花坛藏尸案”一审宣判", titles)
        self.assertNotIn("一个讨厌了很久的健身博主", titles)
        self.assertNotIn("IG老板怒斥AL训练赛放鸽子", titles)
        self.assertNotIn("乌方在三方会谈前表态", titles)
        # 没桥接的热点不开口，不硬蹭
        self.assertNotIn("华为推出全新小折叠电脑", titles)
        self.assertNotIn("江苏姑娘赴河北找到三四块流星陨石", titles)
        # “夺冠”不单独触发运动桥接：电竞夺冠不是跑步
        self.assertNotIn("韩网倒戈力挺BLG夺冠", [o["title"] for o in pick_hook_hots(
            {"top": [{"title": "韩网倒戈力挺BLG夺冠", "platform": "抖音",
                      "hot": 1, "rank": 1}]}, 1)])

    def test_hook_pattern_selection(self):
        # H4 给第一次进馆类选题；H1 只给推销/办卡类选题；教学类选题
        #（减脂、体态）没有对应的句式，返回空，不硬套。
        self.assertEqual(llm._hook_pattern_for(TOPIC)["id"], "H4")
        card = {"title": "办卡前先看这 3 个地方", "angle": "怎么选"}
        self.assertEqual(llm._hook_pattern_for(card)["id"], "H1")
        fat = {"title": "减脂先记3天饮食再谈训练", "angle": "减脂饮食"}
        self.assertIsNone(llm._hook_pattern_for(fat))
        posture = {"title": "圆肩驼背每天练什么", "angle": "体态纠正"}
        self.assertIsNone(llm._hook_pattern_for(posture))

    def test_template_opens_with_hook_pattern_not_hot_bridge(self):
        # 任务书定：口播钩子用 H1–H6，不用热搜桥接。A股还在选题里，但不进首镜。
        # “不用先办卡”已死：那句是商家口号、AI 味，首镜不准再出现。
        # H4 有 4 条写法轮换，首镜只要是其中一条就算过，不锁死某一句。
        t = dict(TOPIC, hook_hot={"title": "A股三大指数集体高开", "platform": "抖音"})
        s = llm.template_script(t, MIN_CFG)
        first = s["scenes"][0]["narration"]
        h4 = [p for p in llm.HOOK_PATTERNS if p["id"] == "H4"][0]["lines"]
        self.assertTrue(any(first.startswith(x) for x in h4))
        self.assertNotIn("不用先办卡", first)
        self.assertNotIn("A股", first)
        self.assertIn("第一次进健身房", first)

    def test_batch_hook_lines_rotate(self):
        # 同一句话不准开半批视频的头：22 条里任何一句首镜最多开 4 条。
        # 上一轮 H4 一句话开了 10 条，换了词没换结构，测试必须拦住。
        from collections import Counter
        from core.common_materials import faq_topics
        teaching = [{**x, "kind": "evergreen", "hot_ref": "", "linked_hot": "",
                     "hook_hot": {}} for x in
                    [{"title": "新手第一次进健身房该怎么练",
                      "hook": "第一次进健身房，别急着加片", "angle": "新手入门",
                      "cover": "别急着加片"},
                     {"title": "圆肩驼背每天练什么",
                      "hook": "靠墙站30秒，先测测你的圆肩", "angle": "体态纠正",
                      "cover": "先测圆肩"},
                     {"title": "减脂先记3天饮食再谈训练",
                      "hook": "减脂先别加练，先记3天吃了什么", "angle": "减脂饮食",
                      "cover": "先记3天"}]]
        faq = [{**x, "kind": "evergreen", "hot_ref": "", "linked_hot": "",
                "hook_hot": {}} for x in faq_topics()]
        scripts = llm.generate_scripts(teaching + faq, MIN_CFG)
        firsts = Counter(s["scenes"][0]["narration"].split("。")[0] + "。"
                         for s in scripts)
        self.assertLessEqual(max(firsts.values()), 4)

    def test_faq_answer_drives_knowledge_and_no_ai_slogan(self):
        # 问答类选题：第 3、4 镜必须答这条问题本身，不再套通用新手练法；
        # 整条文案里不准出现“不用先办卡”。
        from core.common_materials import faq_topics
        topics = faq_topics()
        bag = {t["title"]: t for t in topics}
        t = dict(bag["第一次去健身房要带什么"], kind="evergreen",
                 hot_ref="", linked_hot="", hook_hot={})
        s = llm.template_script(t, MIN_CFG)
        self.assertIn("水壶", s["scenes"][2]["narration"])
        self.assertIn("锁", s["scenes"][2]["narration"])
        self.assertIn("耳机", s["scenes"][3]["narration"])
        self.assertNotIn("2 组", s["scenes"][2]["narration"])
        self.assertNotIn("2 组", s["scenes"][3]["narration"])
        full = s["caption"] + "".join(x["narration"] for x in s["scenes"])
        self.assertNotIn("不用先办卡", full)

    def test_no_script_hits_generic_default_pain(self):
        # 复盘整改：痛点按问题取，22 条里任何一条都不准命中通用默认句。
        from core.common_materials import faq_topics
        teaching = [{**x, "kind": "evergreen", "hot_ref": "", "linked_hot": "",
                     "hook_hot": {}} for x in
                    [{"title": "新手第一次进健身房该怎么练",
                      "hook": "第一次进健身房，别急着加片", "angle": "新手入门",
                      "cover": "别急着加片"},
                     {"title": "圆肩驼背每天练什么",
                      "hook": "靠墙站30秒，先测测你的圆肩", "angle": "体态纠正",
                      "cover": "先测圆肩"},
                     {"title": "减脂先记3天饮食再谈训练",
                      "hook": "减脂先别加练，先记3天吃了什么", "angle": "减脂饮食",
                      "cover": "先记3天"}]]
        faq = [{**x, "kind": "evergreen", "hot_ref": "", "linked_hot": "",
                "hook_hot": {}} for x in faq_topics()]
        scripts = llm.generate_scripts(teaching + faq, MIN_CFG)
        self.assertEqual(len(scripts), 22)
        default = "练了几天没感觉，就开始怀疑这家馆到底适不适合自己。"
        for s in scripts:
            self.assertNotIn(default, s["scenes"][1]["narration"])

    def test_faq_scene2_carries_question_pain(self):
        # 每条 FAQ 稿子的第 2 镜必须是该题自己的 pain；
        # 本地通用痛点（pain_local）不准覆盖问题痛点。
        from core.common_materials import faq_topics
        cfg = dict(MIN_CFG)
        cfg["local"] = {
            "enabled": True, "city": "厦门", "district": "翔安",
            "store": "凌扬运动前浯健身工场", "transit": "地铁4号线前浯站",
            "tags_local": ["#翔安健身"],
            "audience_segments": ["在前浯、南部新城一带上班或活动的人"],
            "audience_anchors": ["第一次进健身房，怕被推销"],
            "pain_local": "怕一进门就被围着推卡，是很多人一直没进来的真实原因。",
            "cta_store": "先来试一次单次体验", "claim_policy": "",
            "no_residence_claim": "", "offer": {"enabled": False},
        }
        for t in faq_topics():
            topic = dict(t, kind="evergreen", hot_ref="", linked_hot="",
                         hook_hot={})
            s = llm.template_script(topic, cfg)
            key = t["pain"].rstrip("。")
            self.assertIn(key, s["scenes"][1]["narration"], t["title"])
            self.assertNotIn("怕一进门就被围着推卡，是很多人一直没进来的真实原因。",
                             s["scenes"][1]["narration"], t["title"])

    def test_template_scenes_2_and_5_carry_numbers_and_distinct_cta(self):
        # 复盘整改：第 2、5 镜要有具体数字，收尾不能三条一样。
        import re
        from core import llm
        cfg = dict(MIN_CFG)
        cfg["local"] = {
            "enabled": True, "city": "厦门", "district": "翔安",
            "store": "凌扬运动前浯健身工场", "transit": "地铁4号线前浯站",
            "tags_local": ["#翔安健身"],
            "audience_segments": ["在前浯、南部新城一带上班或活动的人"],
            "audience_anchors": ["第一次进健身房，怕被推销"],
            "cta_store": "先来试一次单次体验", "claim_policy": "",
            "no_residence_claim": "", "offer": {"enabled": False},
        }
        topics = [
            {"title": "减脂先记3天饮食再谈训练", "hook": "减脂先别加练，先记3天吃了什么",
             "angle": "减脂饮食", "kind": "evergreen", "hot_ref": ""},
            {"title": "新手第一次进健身房该怎么练", "hook": "第一次进健身房，别急着加片",
             "angle": "新手入门", "kind": "evergreen", "hot_ref": ""},
            {"title": "圆肩驼背每天练什么", "hook": "靠墙站30秒，先测测你的圆肩",
             "angle": "体态纠正", "kind": "evergreen", "hot_ref": ""},
        ]
        ctas = []
        for t in topics:
            s = llm.template_script(t, cfg)
            self.assertRegex(s["scenes"][1]["narration"], r"\d", "第 2 镜要有具体数字")
            self.assertRegex(s["scenes"][4]["narration"], r"\d", "第 5 镜要有具体数字")
            ctas.append(s["scenes"][4]["narration"])
        self.assertEqual(len(set(ctas)), 3, "三条收尾不能一样")

    def test_local_micro_area_prefix_and_place_anchor(self):
        # 翔安是大区，标题要细到片区（前浯）；首镜锚点要能轮到片区地名，且不写“住”。
        from core.topics import build_topics, pick_anchor
        from core import llm
        hot = {"fitness_related": [], "bridge_candidates": [],
               "top": [{"title": "A股三大指数集体高开", "platform": "抖音",
                        "hot": 1, "rank": 1, "score": 2.0}]}
        cfg = dict(MIN_CFG)
        cfg["niche"] = dict(MIN_CFG["niche"], evergreen_topics=[
            {"title": "新手第一次进健身房该怎么练", "hook": "第一次进健身房，别急着加片",
             "angle": "新手入门", "cover": "别急着加片"}])
        cfg["local"] = {
            "enabled": True, "city": "厦门", "district": "翔安", "micro_area": "前浯",
            "store": "凌扬运动前浯健身工场", "transit": "地铁4号线前浯站",
            "tags_local": ["#翔安健身"],
            "audience_segments": ["在前浯、南部新城一带上班或活动的人"],
            "audience_anchors": ["在前浯、南部新城一带上班", "在新浦嘉园附近",
                                 "从前浯站出来", "在南部新城 TOD 一带活动",
                                 "在金海路边上", "从厦大翔安校区出来",
                                 "在保利国贸沁园附近", "在蓬莱路边上"],
            "cta_store": "先来试一次单次体验", "claim_policy": "",
            "no_residence_claim": "", "offer": {"enabled": False},
        }
        picked = build_topics(hot, cfg, count=1)
        self.assertTrue(picked[0]["title"].startswith("厦门翔安前浯"))
        # 新手选题命中 H4 句式，首镜不再叠锚点；锚点用没有句式的减脂选题验。
        fat = {"title": "减脂先记3天饮食再谈训练", "hook": "减脂先别加练，先记3天吃了什么",
               "angle": "减脂饮食", "kind": "evergreen", "hot_ref": ""}
        anchor = pick_anchor(fat, cfg["local"])
        self.assertTrue(anchor.startswith(("在", "从")))
        self.assertNotIn("住", anchor)
        s = llm.template_script(fat, cfg)
        self.assertIn(anchor, s["scenes"][0]["narration"])

    def test_pick_hook_hots_excludes_nonlocal_in_local_mode(self):
        from core.topics import pick_hook_hots
        hot = {"top": [
            {"title": "盐城马拉松500米冠军", "platform": "B站运动榜", "hot": 1, "rank": 1},
            {"title": "A股三大指数集体高开", "platform": "抖音", "hot": 1, "rank": 2},
            {"title": "厦门马拉松开跑", "platform": "抖音", "hot": 1, "rank": 3},
        ]}
        local_names = ["厦门", "翔安", "福建"]
        titles = [o["title"] for o in pick_hook_hots(hot, 3, local_names)]
        self.assertNotIn("盐城马拉松500米冠军", titles)
        self.assertIn("A股三大指数集体高开", titles)
        # 绿线是放行不是排除：点了本地名的标题（厦门马拉松）放行；没点任何地名的也放行
        self.assertIn("厦门马拉松开跑", titles)
        # 非本地模式不拦地名：全国号用盐城马拉松没问题
        titles_all = [o["title"] for o in pick_hook_hots(hot, 3, None)]
        self.assertIn("盐城马拉松500米冠军", titles_all)

    def test_hook_hot_used_at_most_once_per_day(self):
        from core.topics import build_topics
        hot = {"fitness_related": [], "bridge_candidates": [],
               "top": [{"title": "A股三大指数集体高开", "platform": "抖音",
                        "hot": 1, "rank": 1, "score": 2.0},
                       {"title": "共绘“健康中国”美好生活图景", "platform": "抖音",
                        "hot": 1, "rank": 2, "score": 1.9}]}
        cfg = dict(MIN_CFG)
        cfg["niche"] = dict(MIN_CFG["niche"], evergreen_topics=[
            {"title": "新手第一次进健身房该怎么练", "hook": "第一次进健身房，别急着加片",
             "angle": "新手入门", "cover": "别急着加片"},
            {"title": "圆肩驼背每天练什么", "hook": "靠墙站30秒，先测测你的圆肩",
             "angle": "体态", "cover": "靠墙站30秒"},
            {"title": "减脂先记3天饮食再谈训练", "hook": "减脂先别加练，先记3天吃了什么",
             "angle": "减脂", "cover": "先记3天饮食"}])
        cfg["local"] = {
            "enabled": True, "city": "厦门", "district": "翔安", "store": "凌扬运动",
            "transit": "地铁4号线前浯站", "tags_local": ["#翔安健身"],
            "audience_segments": ["翔安上班族"], "audience_anchors": ["住在前浯附近"],
            "cta_store": "先来试一次单次体验", "claim_policy": "", "no_residence_claim": "",
        }
        picked = build_topics(hot, cfg, count=3)
        used = [p["hook_hot"]["title"] for p in picked if p.get("hook_hot")]
        # 两个切口只够两条：第三条没有切口，用自己的钩子，不重复蹭 A 股
        self.assertEqual(len(used), 2)
        self.assertEqual(len(set(used)), 2)
        self.assertEqual(sum(1 for p in picked if not p.get("hook_hot")), 1)

    def test_build_topics_prefixes_local_title(self):
        from core.topics import build_topics
        hot = {"fitness_related": [], "bridge_candidates": [],
               "top": [{"title": "A股三大指数集体高开", "platform": "抖音",
                        "hot": 1, "rank": 1, "score": 2.0}]}
        cfg = dict(MIN_CFG)
        cfg["niche"] = dict(MIN_CFG["niche"], evergreen_topics=[
            {"title": "新手第一次进健身房该怎么练", "hook": "第一次进健身房，别急着加片",
             "angle": "新手入门", "cover": "别急着加片"}])
        cfg["local"] = {
            "enabled": True, "city": "厦门", "district": "翔安", "store": "凌扬运动",
            "transit": "地铁4号线前浯站", "tags_local": ["#翔安健身"],
            "audience_segments": ["翔安上班族"], "audience_anchors": ["住在前浯附近"],
            "cta_store": "先来试一次单次体验", "claim_policy": "", "no_residence_claim": "",
        }
        picked = build_topics(hot, cfg, count=1)
        self.assertTrue(picked[0]["title"].startswith("厦门翔安"))
        self.assertEqual(picked[0]["hook_hot"]["title"], "A股三大指数集体高开")
        from core.topics import build_topics
        hot = {"fitness_related": [], "bridge_candidates": [],
               "top": [{"title": "A股三大指数集体高开", "platform": "抖音",
                        "hot": 1, "rank": 1, "score": 2.0}]}
        cfg = dict(MIN_CFG)
        cfg["niche"] = dict(MIN_CFG["niche"], evergreen_topics=[
            {"title": "新手第一次进健身房该怎么练", "hook": "第一次进健身房，别急着加片",
             "angle": "新手入门", "cover": "别急着加片"}])
        picked = build_topics(hot, cfg, count=1)
        self.assertEqual(picked[0]["hook_hot"]["title"], "A股三大指数集体高开")
        self.assertEqual(picked[0]["linked_hot"], "A股三大指数集体高开")

    def test_local_anchor_applies_when_no_hook_pattern(self):
        # 改后：没有 H 句式的选题，有热点也不再跳过锚点——首镜是本地锚点，不是热搜。
        cfg = dict(MIN_CFG)
        cfg["local"] = {
            "enabled": True, "city": "厦门", "district": "翔安", "micro_area": "前浯",
            "store": "凌扬运动前浯健身工场",
            "transit": "地铁4号线前浯站", "community": "翔安新城",
            "tags_local": ["#翔安健身"], "audience_segments": ["翔安上班族"],
            "audience_anchors": ["在新浦嘉园附近"], "cta_store": "先来试一次单次体验",
            "claim_policy": "", "no_residence_claim": "", "offer": {"enabled": False},
        }
        t = {"title": "减脂先记3天饮食再谈训练", "hook": "减脂先别加练，先记3天吃了什么",
             "angle": "减脂饮食", "kind": "evergreen", "hot_ref": "",
             "hook_hot": {"title": "共绘“健康中国”美好生活图景", "platform": "抖音"}}
        s = llm.template_script(t, cfg)
        first = s["scenes"][0]["narration"]
        self.assertTrue(first.startswith("在新浦嘉园附近，"))
        self.assertNotIn("健康中国", first)

    def test_shot_list_from_template_script(self):
        from core.shoot import shot_list, publish_pack
        cfg = dict(MIN_CFG)
        cfg["local"] = {
            "enabled": True, "city": "厦门", "district": "翔安", "micro_area": "前浯",
            "store": "凌扬运动前浯健身工场",
            "transit": "地铁4号线前浯站", "community": "翔安新城",
            "tags_local": ["#翔安健身"], "audience_segments": ["翔安上班族"],
            "audience_anchors": ["在新浦嘉园附近"], "cta_store": "先来试一次单次体验",
            "claim_policy": "", "no_residence_claim": "", "offer": {"enabled": False},
        }
        s = llm.template_script(TOPIC, cfg)
        shots = shot_list(s, cfg)
        self.assertEqual(len(shots), 5)
        self.assertIn("前浯站", shots[0]["location"])
        self.assertIn("器械区", shots[2]["location"])
        for shot in shots:
            self.assertIn("不背词", shot["people"])
            self.assertGreaterEqual(shot["est_seconds"], 5)
        total = sum(x["est_seconds"] for x in shots)
        # 口播时长（不是成片时长：60 秒成片含停顿与画面留白）。
        self.assertGreaterEqual(total, 30)
        self.assertLessEqual(total, 90)
        pack = publish_pack(s, cfg)
        self.assertEqual(pack["title"], s["title"])
        self.assertIsNone(pack["group_buy"])
        draft = pack["jianying_draft"]
        self.assertEqual(len(draft["materials"]["texts"]), 5)
        self.assertEqual(len(draft["tracks"][0]["segments"]), 5)
        self.assertIn("unverified", draft)

    def test_funnel_records_video_and_metrics(self):
        import os
        import tempfile
        from core import funnel
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            conn = funnel.init_db(path)
            funnel.record_video(conn, {"id": "v1", "title": "t", "topic_kind": "evergreen",
                                       "hook_pattern": "H1", "hook_text": "x",
                                       "script_json": "{}", "status": "draft"})
            funnel.record_metrics(conn, "v1", {"plays": 100, "local_share": 0.8,
                                               "poi_clicks": 10, "orders": 2,
                                               "redemptions": 1, "visits": 1,
                                               "signups": 0, "cost_cents": 0,
                                               "source": "douyin"})
            row = funnel.get_funnel(conn, "v1")
            self.assertEqual(row["plays"], 100)
            self.assertEqual(row["redemptions"], 1)
            self.assertEqual(row["title"], "t")
            self.assertEqual(row["hook_pattern"], "H1")
        finally:
            os.unlink(path)

    def test_example_materials_faq_topics_generate_scripts(self):
        # GPT 复核 2026-11-10 第 1、2 项：示例层保证新 clone 可跑。
        # 示例文件是通用健身常识重写，不是门店记录；加载器缺真实文件时回退到它，
        # 并打 is_example 标记；显式传空字典不准偷偷读磁盘。
        import json as _json
        from core import common_materials as cm
        example = _json.loads(cm.EXAMPLE_PATH.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(example["faq"]), 20)
        for f in example["faq"]:
            self.assertTrue(f["source"].startswith("http"))
            self.assertTrue(f["topic"]["title"] and f["topic"]["hook"])
        self.assertGreaterEqual(len(example["competitors_xiamen"]), 5)
        for c in example["competitors_xiamen"]:
            self.assertTrue(c["name"])
        # 回退：真实文件缺失时读示例，并标记 is_example。
        with mock.patch.object(cm, "PATH", Path("/nonexistent/common_materials.json")):
            m = cm.load_common_materials()
            self.assertTrue(m["is_example"])
            self.assertEqual(cm.materials_source(), "example")
            topics = cm.faq_topics()
        self.assertEqual(len(topics), len(example["faq"]) - 1)
        self.assertFalse(any("团课" in t["title"] for t in topics))
        # 显式空字典不读盘：load 被 mock 成抛错也必须返回空选题。
        with mock.patch.object(cm, "load_common_materials",
                               side_effect=AssertionError("不准读盘")):
            self.assertEqual(cm.faq_topics({}), [])
        t = {**topics[0], "kind": "evergreen", "hot_ref": "", "linked_hot": "",
             "hook_hot": {}}
        s = llm.template_script(t, MIN_CFG)
        self.assertEqual(len(s["scenes"]), 5)
        self.assertIn("水壶毛巾锁", s["scenes"][0]["narration"])
        self.assertNotIn("9.9", s["caption"])
        self.assertNotIn("239", s["caption"])

    def test_real_materials_faq_topics_generate_scripts(self):
        # 真实数据层：只在真实素材存在时运行；缺失时显式 skip，不静默跳过。
        from core.common_materials import PATH as REAL_PATH
        from core.common_materials import faq_topics, load_common_materials
        if not REAL_PATH.exists():
            self.skipTest("真实素材缺失：data/baseline/common_materials.json "
                          "不在仓库中；本测试仅在有真实数据时运行"
                          "（示例层见 test_example_materials_...）")
        # 共性素材：FAQ 至少 20 条，每条有来源；市场价只做参考，绝不进文案。
        m = load_common_materials()
        self.assertFalse(m["is_example"])
        self.assertGreaterEqual(len(m["faq"]), 20)
        for f in m["faq"]:
            self.assertTrue(f["source"].startswith("http"))
            self.assertTrue(f["topic"]["title"] and f["topic"]["hook"])
        self.assertGreaterEqual(len(m["competitors_xiamen"]), 5)
        for c in m["competitors_xiamen"]:
            self.assertTrue(c["source"].startswith("http"))
        topics = faq_topics(m)
        # 团课题因本店没有课表被排除：20 条里出 19 条。
        self.assertEqual(len(topics), len(m["faq"]) - 1)
        self.assertFalse(any("团课" in t["title"] for t in topics))
        t = {**topics[0], "kind": "evergreen", "hot_ref": "", "linked_hot": "",
             "hook_hot": {}}
        s = llm.template_script(t, MIN_CFG)
        self.assertEqual(len(s["scenes"]), 5)
        self.assertIn("水壶毛巾锁", s["scenes"][0]["narration"])
        self.assertNotIn("9.9", s["caption"])
        self.assertNotIn("239", s["caption"])

    def test_example_materials_marked_in_script_metadata(self):
        """GPT 第二次复核第 1 项跟进：示例素材状态必须随生成物走 ——
        faq_topics 给每条选题打 materials_source，template_script 把
        topic 原样写进脚本的 topic 字段，标记随产物走；回退时日志提示
        在 load_common_materials 里已打。"""
        import json as _json
        from core import common_materials as cm
        with mock.patch.object(cm, "PATH", Path("/nonexistent/common_materials.json")):
            topics = cm.faq_topics()
        self.assertTrue(all(t["materials_source"] == "example" for t in topics))
        t = {**topics[0], "kind": "evergreen", "hot_ref": "", "linked_hot": "",
             "hook_hot": {}}
        s = llm.template_script(t, MIN_CFG)
        self.assertEqual(s["topic"]["materials_source"], "example")
        # 对照：真实文件存在时标记为 real（本机真实文件在，不用 mock）。
        if cm.PATH.exists():
            real_topics = cm.faq_topics()
            self.assertTrue(all(t["materials_source"] == "real"
                                for t in real_topics))

    def test_walkthrough_t3_script_and_pack(self):
        # T3 动线实拍（仓库转译库）：自己的钩子就是开头，不套 H 句式，不加锚点。
        from core.shoot import publish_pack
        cfg = dict(MIN_CFG)
        cfg["local"] = {
            "enabled": True, "city": "厦门", "district": "翔安", "micro_area": "前浯",
            "store": "凌扬运动前浯健身工场",
            "transit": "地铁4号线前浯站", "community": "翔安新城",
            "tags_local": ["#翔安健身"], "audience_segments": ["翔安上班族"],
            "audience_anchors": ["在新浦嘉园附近"], "cta_store": "先来试一次单次体验",
            "claim_policy": "", "no_residence_claim": "", "offer": {"enabled": False},
        }
        t3 = {"title": "不用背词，带你从前浯站走到器械区",
              "hook": "不用背词，镜头从 4 号线前浯站一路走到器械区，今天什么样直接看。",
              "angle": "第一人称沉浸式实拍还原完整动线",
              "kind": "evergreen", "hook_mode": "self_contained", "cover": "进门实拍",
              "hot_ref": ""}
        s = llm.template_script(t3, cfg)
        first = s["scenes"][0]["narration"]
        self.assertTrue(first.startswith("不用背词，镜头从 4 号线前浯站一路走到器械区"))
        pack = publish_pack(s, cfg)
        self.assertEqual(len(pack["shots"]), 5)
        self.assertIn("前浯站", pack["shots"][0]["location"])
        self.assertIn("前浯站", pack["poi"]["transit"])
        self.assertNotIn("住在前浯附近", first)
        self.assertLessEqual(len(first), 60)
        self.assertEqual(s["scenes"][0]["sub"], "翔安 · 60 秒讲清楚")
        self.assertIn("#翔安健身", s["caption"])


class TestFunnel(unittest.TestCase):
    """GPT 第二次复核第 5 节第 4 项：外键生效、未知指标记 NULL、
    重复记视频用 UPSERT 不重建记录。"""

    def setUp(self):
        import os
        import tempfile
        from core import funnel
        self.funnel = funnel
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.conn = funnel.init_db(self.path)

    def tearDown(self):
        import os
        self.conn.close()
        os.unlink(self.path)

    def _video(self, vid="v1", title="t"):
        self.funnel.record_video(self.conn, {"id": vid, "title": title,
                                             "topic_kind": "evergreen",
                                             "hook_pattern": "H1",
                                             "hook_text": "x",
                                             "script_json": "{}",
                                             "status": "draft"})

    def test_foreign_key_enforced(self):
        """不存在的视频记指标时，外键约束必须拦住，不静默写孤儿行。"""
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            self.funnel.record_metrics(self.conn, "no-such-video",
                                       {"plays": 1})

    def test_unknown_metrics_are_null_not_zero(self):
        """只给了 plays，其余指标读回必须是 NULL，不是 0 ——
        “不知道”不是“零播放”。"""
        self._video()
        self.funnel.record_metrics(self.conn, "v1", {"plays": 100})
        row = self.funnel.get_funnel(self.conn, "v1")
        self.assertEqual(row["plays"], 100)
        for field in ("local_share", "poi_clicks", "orders", "redemptions",
                      "visits", "signups", "cost_cents"):
            self.assertIsNone(row[field], f"{field} 应为 NULL，不是 0")

    def test_repeated_video_upsert_keeps_metrics(self):
        """同一视频重复记入时只更新字段：旧指标行不丢，标题更新。"""
        self._video(title="旧标题")
        self.funnel.record_metrics(self.conn, "v1", {"plays": 100})
        self._video(title="新标题")
        row = self.funnel.get_funnel(self.conn, "v1")
        self.assertEqual(row["title"], "新标题")
        self.assertEqual(row["plays"], 100)


class TestKnowledgeFor(unittest.TestCase):
    """GPT 第二次复核第 3 节：答案切分遇上异常输入（非字符串、空串、
    无标点）不崩，按无答案走通用路径。"""

    def test_non_string_answer_is_ignored(self):
        t = {**TOPIC, "answer": 12345}
        s = llm.template_script(t, MIN_CFG)
        self.assertEqual(len(s["scenes"]), 5)

    def test_empty_answer_is_ignored(self):
        t = {**TOPIC, "answer": ""}
        s = llm.template_script(t, MIN_CFG)
        self.assertEqual(len(s["scenes"]), 5)

    def test_answer_without_punctuation_is_ignored(self):
        t = {**TOPIC, "answer": "一句话没标点"}
        s = llm.template_script(t, MIN_CFG)
        self.assertEqual(len(s["scenes"]), 5)


class TestSidecarSpec(unittest.TestCase):
    """GPT 复核 2026-11-10 第 3 项：sidecar 只打包白名单示例数据，
    data/baseline 整个目录永不进包。"""

    def test_spec_bundles_only_example_baseline(self):
        root = Path(__file__).resolve().parents[1]
        spec = (root / "desktop" / "sidecar" / "lynvos.spec").read_text(encoding="utf-8")
        self.assertNotIn('"data" / "baseline"', spec,
                         "spec 仍在打包整个 data/baseline 目录")
        self.assertIn('"examples" / "baseline"', spec)
        for name in ("method_playbook.example.json", "local_benchmark.example.json",
                     "offer_presets.example.json", "common_materials.example.json"):
            self.assertTrue((root / "examples" / "baseline" / name).is_file(),
                            f"白名单示例文件缺失：{name}")


if __name__ == "__main__":
    unittest.main()
