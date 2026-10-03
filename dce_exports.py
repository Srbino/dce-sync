"""Standalone export options. Never used to advance an archive checkpoint."""
import re

FORMATS = {'Json': '.json', 'HtmlDark': '.html', 'HtmlLight': '.html',
           'Csv': '.csv', 'PlainText': '.txt'}
DEFAULTS = dict(format='HtmlDark', after='', before='', filter='', partition='', locale='', reverse=False)


def validate(values):
    if not isinstance(values, dict) or set(values) - DEFAULTS.keys():
        raise ValueError('Invalid export options.')
    result = dict(DEFAULTS, **values)
    if result['format'] not in FORMATS or type(result['reverse']) is not bool:
        raise ValueError('Invalid export format or ordering.')
    for key in ('after', 'before', 'filter', 'partition', 'locale'):
        value = result[key]
        if not isinstance(value, str) or len(value) > 2000 or any(ord(c) < 32 for c in value):
            raise ValueError(f'Invalid {key}.')
        result[key] = value.strip()
    if result['partition'] and not re.fullmatch(r'[1-9]\d*(?:\.\d+)?(?:b|kb|mb|gb)?', result['partition'], re.I):
        raise ValueError('Partition must be a message count or size, such as 10000 or 20mb.')
    if result['locale'] and not re.fullmatch(r'[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', result['locale']):
        raise ValueError('Use a locale such as en-US or cs-CZ.')
    return result


def arguments(options):
    result = ['-f', options['format'], '--reverse', str(options['reverse']).lower()]
    for name in ('after', 'before', 'filter', 'partition', 'locale'):
        if options[name]:
            result += ['--' + name, options[name]]
    return result
