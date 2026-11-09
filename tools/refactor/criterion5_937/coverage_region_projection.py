"""937 pinned static region/projection checker SOURCE ONLY, UNEXECUTED/unwired.

No native parser, report generation, graph/source/producer authentication or
statement/region coverage verdict. Actual owner must retain catalog bytes/tool
and independently establish source/raw graph/cleanup/linkage before use.
"""
import hashlib
import json

from .suite_evidence import B_PIN

REGION_FORMAT = 'criterion5-937-static-region-identities-v1'
REGION_BYTES = 371842
REGION_SHA256 = '303bb289ae064040887fe9286961f2675758b7d59d5412dc26dde0968fbbd6c5'
SOURCE_CATALOG_SHA256 = 'c060901c910043552254a72d5c727646a84ac77bbca58a7bc61d10ea685d9f19'
TOOL_ARCHIVE_SHA256 = '49d58df8284f40cb16b781d5fc8b1d4fdefe185ce0c691e39e4b73d1f83c575b'


def _load_pinned_catalog(raw, tool_archive_sha256):
    if (type(raw) is not bytes or len(raw) != REGION_BYTES
            or hashlib.sha256(raw).hexdigest() != REGION_SHA256
            or type(tool_archive_sha256) is not str or tool_archive_sha256 != TOOL_ARCHIVE_SHA256):
        raise ValueError('exact retained region catalog/tool binding required')
    # Only this exact source-reviewed finite JSON byte string reaches decode;
    # no untrusted alternative catalog/normalization/default tool is accepted.
    value = json.loads(raw)
    if (value['format'] != REGION_FORMAT or value['pin'] != B_PIN
            or value['catalog_sha256'] != SOURCE_CATALOG_SHA256):
        raise ValueError('frozen region catalog format/source binding mismatch')
    return value  # source identity data, NOT native parser/source authentication


def _check_projection(report, *, inventory, regions):
    """Conditional pure record algorithm; lower fixtures do NOT pin/load bytes.

    Measurement callers must use validate_region_projection, whose loader pins
    the complete catalog/tool before invoking this helper. No alternate profile.
    """
    if (type(inventory) is not dict or inventory['pin'] != B_PIN
            or type(inventory['files']) is not dict or not inventory['files']
            or type(regions) is not dict or regions['pin'] != B_PIN
            or regions['format'] != REGION_FORMAT
            or regions['catalog_sha256'] != SOURCE_CATALOG_SHA256
            or type(regions['files']) is not dict):
        raise ValueError('complete matching source/region declarations required')
    if type(report) is not dict or type(report.get('meta')) is not dict:
        raise ValueError('native JSON report header required')
    meta = report['meta']
    if (type(meta.get('format')) is not int or meta['format'] != 3
            or meta.get('version') != '7.17.0' or meta.get('branch_coverage') is not True
            or meta.get('show_contexts') is not False):
        raise ValueError('single frozen false-context native report profile required')
    expected = set(inventory['files'])
    if (type(report.get('files')) is not dict or set(report['files']) != expected
            or set(regions['files']) != expected):
        raise ValueError('missing/extra source or region/report file')
    functions, classes = 0, 0
    for name, source in inventory['files'].items():
        identities, row = regions['files'][name], report['files'][name]
        if (type(identities) is not dict or type(row) is not dict
                or type(source['sha256']) is not str
                or type(identities['source_sha256']) is not str
                or identities['source_sha256'] != source['sha256']
                or type(source['physical_lines']) is not int or source['physical_lines'] < 0
                or type(identities['physical_lines']) is not int
                or identities['physical_lines'] != source['physical_lines']):
            raise ValueError('source/region identity declaration mismatch')
        if 'contexts' in row:
            raise ValueError('file context map forbidden; full attribution stays in graph')
        for kind in ('functions', 'classes'):
            wanted, actual = identities[kind], row.get(kind)
            if (type(wanted) is not dict or type(actual) is not dict
                    or any(type(key) is not str or not key for key in wanted)
                    or set(actual) != set(wanted) | {''}):
                raise ValueError('missing/foreign native named or outside region')
            for key, region in actual.items():
                if type(region) is not dict or 'contexts' in region:
                    raise ValueError('region context map/shape outside frozen projection')
                start = region.get('start_line')
                if type(start) is not int or not 1 <= start <= max(1, source['physical_lines']):
                    raise ValueError('invalid typed native region start')
                if key:
                    target = wanted[key]
                    if (type(target) is not int or not 1 <= target <= source['physical_lines']
                            or start != target):
                        raise ValueError('native named-region start differs from static identity')
                # Outside start stays native/bounded: never infer membership or
                # discard the overwritten getter to compute outside lines.
            if kind == 'functions':
                functions += len(wanted)
            else:
                classes += len(wanted)
    return {'source_file_count': len(expected), 'named_function_count': functions,
            'named_class_count': classes,
            'qualification': 'STATIC_REGION_IDENTITY_AND_PROJECTION_ONLY_NOT_COVERAGE_ACCEPTANCE',
            'actual_source_graph_producer_or_coverage_accepted': False}


def validate_region_projection(report, *, inventory, region_catalog_raw, tool_archive_sha256):
    regions = _load_pinned_catalog(region_catalog_raw, tool_archive_sha256)
    return _check_projection(report, inventory=inventory, regions=regions)
