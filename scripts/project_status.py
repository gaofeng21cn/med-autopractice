"""只读展示 MAP 骨架与计划状态；不连接设备、模型或医院系统。"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys

from package_metadata import verify_package

ROOT = Path(__file__).resolve().parents[1]


def read_json(relative):
    path = (ROOT / relative).resolve()
    if not path.is_relative_to(ROOT):
        raise ValueError('元数据引用超出项目目录')
    return json.loads(path.read_text())


def inspect_project():
    package = verify_package(ROOT)
    manifest = read_json('agent/manifest.json')
    for key in ('capabilities_ref', 'contract_ref', 'skill_ref', 'plugin_ref', 'knowledge_ref'):
        path = (ROOT / manifest[key]).resolve()
        if not path.is_relative_to(ROOT) or not path.is_file():
            raise ValueError(f'无效项目引用: {key}')
    plugin = read_json(manifest['plugin_ref'])
    if plugin['version'] != manifest['version']:
        raise ValueError('插件与 Agent 清单版本不一致')
    catalog = read_json(manifest['capabilities_ref'])
    capabilities = catalog['capabilities']
    ids = [item['id'] for item in capabilities]
    if len(ids) != len(set(ids)):
        raise ValueError('能力 ID 重复')
    for item in capabilities:
        if type(item['callable']) is not bool:
            raise ValueError(f'能力可用性不是布尔值: {item["id"]}')
        if item['status'] == 'planned' and item['callable']:
            raise ValueError(f'计划能力不能声明已可调用: {item["id"]}')
        if item['implementation_ref'] is not None:
            path = (ROOT / item['implementation_ref']).resolve()
            if not path.is_relative_to(ROOT) or not path.exists():
                raise ValueError(f'实现引用不存在: {item["id"]}')
    callable_ids = [item['id'] for item in capabilities if item['callable']]
    if catalog.get('clinical_execution_qualified') is not False:
        raise ValueError('能力目录不能提前声明临床验收')
    if not set(catalog.get('first_pilot', [])) <= set(ids):
        raise ValueError('首批任务不在能力目录中')
    if not catalog['clinical_execution_qualified'] and (manifest['clinical_execution_available'] or callable_ids):
        raise ValueError('骨架状态不能声明临床执行已可用')
    return {
        'package': package,
        'product': manifest['display_name'], 'version': manifest['version'],
        'status': manifest['status'], 'clinical_execution_available': manifest['clinical_execution_available'],
        'independent_ui': manifest['independent_ui'], 'integration': manifest['integration'],
        'capabilities_by_phase': dict(Counter(item['phase'] for item in capabilities)),
        'current_backend': catalog['current_backend'],
        'site_transports': catalog['site_transports'],
        'first_pilot': catalog['first_pilot'],
        'callable_capabilities': callable_ids,
        'local_implementation_capabilities': [item['id'] for item in capabilities if item['status'] == 'local_implementation'],
        'validation_scope': 'Local metadata references and consistency only; not clinical qualification',
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true', help='输出机器可读状态')
    args = parser.parse_args()
    try:
        result = inspect_project()
    except (OSError, ValueError, KeyError, TypeError) as error:
        print(f'项目元数据检查失败: {error}', file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print(f'{result["product"]} {result["version"]}: {result["status"]}')
        print('能力规划:', result['capabilities_by_phase'])
        print('临床执行可用:', result['clinical_execution_available'])
        print('可调用能力:', result['callable_capabilities'])
        print('此命令只查看项目，不执行临床任务。')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
