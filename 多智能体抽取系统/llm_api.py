# -*- coding: utf-8 -*-
"""
大模型 API 调用封装（天大校园端点版）。

调用 ai.tju.edu.cn 的 OpenAI 兼容接口（tju-llm，vLLM 后端）。
所有模块统一通过本文件调用大模型，便于维护和配置。

用法:
    import llm_api
    text = llm_api.chat(messages, max_tokens=4096, temperature=0.6)
    results = llm_api.batch_chat(messages_list, max_tokens=4096, temperature=0.6)
"""
import os
import time
from pathlib import Path

import requests
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# 配置区
# ============================================================
API_URL = "https://ai.tju.edu.cn/api/v3/chat/completions"
MODEL_NAME = "tju-llm"


def _load_api_key():
    """API Key 不入库：优先环境变量 TUJU_API_KEY，其次本目录 api_key.txt（已被 .gitignore 排除）。"""
    key = os.environ.get("TUJU_API_KEY", "").strip()
    if key:
        return key
    key_file = Path(__file__).resolve().parent / "api_key.txt"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8-sig").strip()
    return ""


API_KEY = _load_api_key()

# 默认采样参数
DEFAULT_MAX_TOKENS = 4096
DEFAULT_TEMPERATURE = 0.6
DEFAULT_TOP_P = 0.9

# 请求超时（秒），大模型生成长文本时可能较慢
REQUEST_TIMEOUT = 300

# 批量调用的默认并发数
DEFAULT_MAX_WORKERS = 100

# 失败重试次数
MAX_RETRIES = 3
RETRY_DELAY = 5  # 重试间隔（秒）
# ============================================================


def _build_headers():
    # OpenAI 兼容接口：Authorization 头携带 Bearer 前缀的令牌
    return {
        "Authorization": f"Bearer {API_KEY}",
        "Content-Type": "application/json",
    }


def chat(messages, max_tokens=DEFAULT_MAX_TOKENS, temperature=DEFAULT_TEMPERATURE,
         top_p=DEFAULT_TOP_P, timeout=REQUEST_TIMEOUT):
    """单次对话补全调用。

    Args:
        messages: OpenAI 格式的消息列表，如
            [{"role": "system", "content": "..."}, {"role": "user", "content": "..."}]
        max_tokens: 最大生成 token 数
        temperature: 采样温度
        top_p: nucleus sampling 概率
        timeout: 请求超时秒数

    Returns:
        str: 模型生成的文本内容
    """
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
    }
    headers = _build_headers()

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(API_URL, json=payload, headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            last_error = e
            print(f"  [API 警告] 第 {attempt}/{MAX_RETRIES} 次调用失败: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    raise RuntimeError(f"API 调用失败（已重试 {MAX_RETRIES} 次）: {last_error}")


def _chat_with_usage(messages, max_tokens=DEFAULT_MAX_TOKENS, temperature=DEFAULT_TEMPERATURE,
                     top_p=DEFAULT_TOP_P, timeout=REQUEST_TIMEOUT):
    """单次对话补全调用，额外返回 token 用量。

    Returns:
        (text, completion_tokens): 生成的文本和生成的 token 数
    """
    payload = {
        "model": MODEL_NAME,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
        "top_p": top_p,
    }
    headers = _build_headers()

    last_error = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.post(API_URL, json=payload, headers=headers, timeout=timeout)
            resp.raise_for_status()
            data = resp.json()
            text = data["choices"][0]["message"]["content"]
            # OpenAI 兼容接口返回 usage.completion_tokens（生成 token 数）
            usage = data.get("usage", {})
            completion_tokens = usage.get("completion_tokens", 0)
            return text, completion_tokens
        except Exception as e:
            last_error = e
            print(f"  [API 警告] 第 {attempt}/{MAX_RETRIES} 次调用失败: {e}")
            if attempt < MAX_RETRIES:
                time.sleep(RETRY_DELAY)

    raise RuntimeError(f"API 调用失败（已重试 {MAX_RETRIES} 次）: {last_error}")


def batch_chat(messages_list, max_tokens=DEFAULT_MAX_TOKENS, temperature=DEFAULT_TEMPERATURE,
               top_p=DEFAULT_TOP_P, max_workers=DEFAULT_MAX_WORKERS, timeout=REQUEST_TIMEOUT):
    """批量并发调用对话补全接口。

    使用线程池并发发送请求，保持与原 vLLM llm.generate() 类似的批量推理语义。

    Args:
        messages_list: 消息列表的列表，每个元素是一组 messages
        max_tokens: 最大生成 token 数
        temperature: 采样温度
        top_p: nucleus sampling 概率
        max_workers: 最大并发数
        timeout: 每个请求的超时秒数

    Returns:
        list[str]: 与 messages_list 等长的文本列表，顺序对应
    """
    results = [None] * len(messages_list)

    def _call(idx, msgs):
        return idx, chat(msgs, max_tokens=max_tokens, temperature=temperature,
                         top_p=top_p, timeout=timeout)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_call, i, msgs): i
            for i, msgs in enumerate(messages_list)
        }
        for future in as_completed(futures):
            idx, text = future.result()
            results[idx] = text

    return results


def batch_chat_with_progress(messages_list, max_tokens=DEFAULT_MAX_TOKENS,
                             temperature=DEFAULT_TEMPERATURE, top_p=DEFAULT_TOP_P,
                             max_workers=DEFAULT_MAX_WORKERS, timeout=REQUEST_TIMEOUT,
                             desc="推理"):
    """带进度条的批量并发调用。

    所有请求一次性提交到线程池，通过 as_completed 逐个完成时更新进度条，
    避免分批等待导致的"一个慢请求阻塞整批"问题。

    进度条会显示实时 token/s 吞吐率（基于已完成请求的累计生成 token 数）。

    Args:
        同 batch_chat，额外:
        desc: 进度条描述文字

    Returns:
        list[str]: 与 messages_list 等长的文本列表
    """
    from tqdm import tqdm

    results = [None] * len(messages_list)
    total_tokens = 0
    start_time = time.time()

    def _call(idx, msgs):
        text, completion_tokens = _chat_with_usage(
            msgs, max_tokens=max_tokens, temperature=temperature,
            top_p=top_p, timeout=timeout)
        return idx, text, completion_tokens

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_call, i, msgs): i
            for i, msgs in enumerate(messages_list)
        }
        with tqdm(total=len(futures), desc=desc, ncols=100) as pbar:
            for future in as_completed(futures):
                idx, text, completion_tokens = future.result()
                results[idx] = text
                total_tokens += completion_tokens
                elapsed = time.time() - start_time
                tps = total_tokens / elapsed if elapsed > 0 else 0
                pbar.set_postfix_str(f"{total_tokens} tok, {tps:.0f} tok/s")
                pbar.update(1)

    elapsed = time.time() - start_time
    tps = total_tokens / elapsed if elapsed > 0 else 0
    print(f"{desc}完成: {total_tokens} tokens, {elapsed:.1f}s, {tps:.0f} tok/s")

    return results


def batch_chat_with_handler(messages_list, handler, max_tokens=DEFAULT_MAX_TOKENS,
                            temperature=DEFAULT_TEMPERATURE, top_p=DEFAULT_TOP_P,
                            max_workers=DEFAULT_MAX_WORKERS, timeout=REQUEST_TIMEOUT,
                            desc="推理", continue_on_error=False, error_handler=None):
    """带进度条的批量并发调用，每个结果完成后由独立消费者线程处理。

    采用生产者-消费者模式：
    - 主线程（生产者）：并发发送 API 请求，结果返回后放入队列，更新进度条
    - 消费者线程：从队列取出结果，调用 handler(idx, text) 进行解析+保存

    这样保存操作不会阻塞进度条更新，且通过 idx 保证结果与请求准确对应。

    Args:
        messages_list: 消息列表的列表，每个元素是一组 messages
        handler: 回调函数 handler(idx, text)，idx 为消息索引，text 为生成文本
        max_tokens: 最大生成 token 数
        temperature: 采样温度
        top_p: nucleus sampling 概率
        max_workers: 最大并发数
        timeout: 每个请求的超时秒数
        desc: 进度条描述文字
        continue_on_error: 单个请求失败时是否继续处理其他请求
        error_handler: 请求失败回调 error_handler(idx, exc)
    """
    import queue
    import threading
    from tqdm import tqdm

    total_tokens = 0
    start_time = time.time()
    result_queue = queue.Queue()

    def _call(idx, msgs):
        text, completion_tokens = _chat_with_usage(
            msgs, max_tokens=max_tokens, temperature=temperature,
            top_p=top_p, timeout=timeout)
        return idx, text, completion_tokens

    # 消费者线程：从队列取结果，调用 handler 解析+保存
    def _consumer():
        while True:
            item = result_queue.get()
            if item is None:  # 哨兵信号，结束消费
                result_queue.task_done()
                break
            idx, text = item
            try:
                handler(idx, text)
            except Exception as e:
                print(f"  [保存异常] idx={idx}: {e}")
            result_queue.task_done()

    consumer_thread = threading.Thread(target=_consumer, daemon=True)
    consumer_thread.start()

    # 生产者：并发发送请求，结果入队
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {
            executor.submit(_call, i, msgs): i
            for i, msgs in enumerate(messages_list)
        }
        with tqdm(total=len(futures), desc=desc, ncols=100) as pbar:
            for future in as_completed(futures):
                idx = futures[future]
                try:
                    idx, text, completion_tokens = future.result()
                except Exception as e:
                    print(f"  [API 失败] idx={idx}: {e}")
                    if error_handler is not None:
                        try:
                            error_handler(idx, e)
                        except Exception as handler_error:
                            print(f"  [失败处理异常] idx={idx}: {handler_error}")
                    pbar.update(1)
                    if continue_on_error:
                        continue
                    raise
                # 结果入队，由消费者线程处理（解析+保存）
                result_queue.put((idx, text))
                total_tokens += completion_tokens
                elapsed = time.time() - start_time
                tps = total_tokens / elapsed if elapsed > 0 else 0
                pbar.set_postfix_str(f"{total_tokens} tok, {tps:.0f} tok/s")
                pbar.update(1)

    # 等待所有结果处理完毕
    result_queue.put(None)  # 哨兵
    result_queue.join()
    consumer_thread.join()

    elapsed = time.time() - start_time
    tps = total_tokens / elapsed if elapsed > 0 else 0
    print(f"{desc}完成: {total_tokens} tokens, {elapsed:.1f}s, {tps:.0f} tok/s")
