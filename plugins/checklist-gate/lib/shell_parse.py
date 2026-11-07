"""Small shell lexer: splits a command line into simple commands, unwrapping wrappers."""

import os
import re
from typing import List, Optional, Tuple

# Script nesting (bash -c, eval, env -S, git !alias) is unwrapped at most this deep.
_MAX_UNWRAP_DEPTH = 8
# Prefix commands (env, command, ...) chained deeper than this are not read.
_MAX_PREFIX_CHAIN = 64
# Reading options both ways can multiply the forms; past this many the rest is not read.
_MAX_FORMS = 1000
# Substitution nesting limit in the lexer.
_MAX_LEX_LEVEL = 40

OPAQUE = '\x00opaque'

_ENV_ASSIGNMENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')
_GIT_ALIAS_OPTION = re.compile(r'^alias\.([^=]+)=(.*)$', re.DOTALL | re.IGNORECASE)
_GIT_OPTIONS_WITH_ARG = {
    '-C', '-c', '--git-dir', '--work-tree', '--namespace', '--super-prefix', '--config-env',
    '--attr-source',
}
_RESERVED_WORDS = {
    '!', '{', '}', 'if', 'then', 'else', 'elif', 'fi', 'do', 'done', 'while', 'until', 'time',
}
_SHELLS = {'bash', 'sh', 'zsh', 'dash', 'ksh'}
_SHELL_VALUE_OPTIONS = {'-o', '+o', '-O', '+O', '--rcfile', '--init-file'}
_XARGS_VALUE_OPTIONS = (
    '-a', '-d', '-E', '-e', '-I', '-L', '-l', '-n', '-P', '-s', '--arg-file', '--delimiter',
    '--eof', '--replace', '--max-lines', '--max-args', '--max-procs', '--max-chars',
)
# command -> (options that take a value, positional arguments to skip)
_PREFIX_COMMANDS = {
    'env': (('-u', '--unset', '-C', '--chdir', '-P', '-L', '-U'), 0),
    '-': ((), 0),
    'caffeinate': (('-t', '-w'), 0),
    'arch': ((), 0),
    'xcrun': (('--sdk', '--toolchain'), 0),
    'command': ((), 0),
    'builtin': ((), 0),
    'nohup': ((), 0),
    'noglob': ((), 0),
    'nocorrect': ((), 0),
    'coproc': ((), 0),
    'repeat': ((), 1),
    'exec': (('-a',), 0),
    'nice': (('-n', '--adjustment'), 0),
    'timeout': (('-s', '--signal', '-k', '--kill-after'), 1),
    'sudo': (('-u', '-g', '-C', '-D', '-h', '-p', '-r', '-t', '-U', '-T'), 0),
    'xargs': (_XARGS_VALUE_OPTIONS, 0),
    'stdbuf': (('-i', '-o', '-e'), 0),
    'time': (('-f', '--format', '-o', '--output'), 0),
}

_REDIRECTS_LT = ('<<<', '<<-', '<<', '<>', '<&', '<')
_REDIRECTS_GT = ('>>', '>|', '>&', '>')


class _ParseError(Exception):
    pass


class _Word:
    __slots__ = ('parts', 'quoted', 'subs')

    def __init__(self) -> None:
        self.parts: List[str] = []
        self.quoted = False
        self.subs: list = []

    def add(self, text: str, quoted: bool = False) -> None:
        self.parts.append(text)
        if quoted:
            self.quoted = True

    @property
    def text(self) -> str:
        return ''.join(self.parts)


class _Tok:
    """kind: 'W' word, 'C' control operator, 'R' redirect operator."""

    __slots__ = ('kind', 'text', 'quoted', 'subs', 'body', 'body_subs', 'delim')

    def __init__(self, kind: str, text: str, quoted: bool = False, subs: Optional[list] = None):
        self.kind = kind
        self.text = text
        self.quoted = quoted
        self.subs = subs or []
        self.body: Optional[str] = None
        self.body_subs: list = []
        self.delim: Optional['_Tok'] = None


class _Lexer:
    def __init__(self, text: str, level: int = 0) -> None:
        self.s = text
        self.i = 0
        self.level = level

    # --- substitutions ---

    def _nested(self) -> list:
        self.level += 1
        if self.level > _MAX_LEX_LEVEL:
            raise _ParseError('nesting too deep')
        try:
            return self.lex(True)
        finally:
            self.level -= 1

    def _backtick(self, word: _Word) -> None:
        s, n = self.s, len(self.s)
        self.i += 1
        buf = []
        while True:
            if self.i >= n:
                raise _ParseError('unclosed backtick')
            c = s[self.i]
            if c == '`':
                self.i += 1
                break
            if c == '\\' and self.i + 1 < n and s[self.i + 1] in '`\\$':
                buf.append(s[self.i + 1])
                self.i += 2
                continue
            buf.append(c)
            self.i += 1
        if self.level + 1 > _MAX_LEX_LEVEL:
            raise _ParseError('nesting too deep')
        word.subs.append(_Lexer(''.join(buf), self.level + 1).lex(False))
        word.add('$(...)')

    def _arith(self, word: _Word) -> None:
        s, n = self.s, len(self.s)
        word.add('$((')
        self.i += 3
        depth = 0
        while True:
            if self.i >= n:
                raise _ParseError('unclosed arithmetic expansion')
            c = s[self.i]
            if c == '(':
                depth += 1
            elif c == ')':
                if depth == 0:
                    if not s.startswith('))', self.i):
                        raise _ParseError('ambiguous $((')
                    word.add('))')
                    self.i += 2
                    return
                depth -= 1
            elif c == '$':
                self._dollar(word, True)
                continue
            elif c == '`':
                self._backtick(word)
                continue
            word.add(c)
            self.i += 1

    def _braced(self, word: _Word, in_dq: bool) -> None:
        s, n = self.s, len(self.s)
        word.add('${')
        self.i += 2
        flags_end = s.find(')', self.i) if s.startswith('(', self.i) else -1
        # zsh's (e) flag evaluates the expansion, so quoted text inside can run commands
        evaluates = flags_end != -1 and 'e' in s[self.i + 1:flags_end]
        while True:
            if self.i >= n:
                raise _ParseError('unclosed ${')
            c = s[self.i]
            if c == '\\':
                word.add(s[self.i:self.i + 2])
                self.i += 2
            elif c == '$' and s.startswith('${', self.i):
                self._braced(word, in_dq)
            elif c == '$':
                self._dollar(word, True)
            elif c == '`':
                self._backtick(word)
            elif c == "'" and evaluates:
                j = s.find("'", self.i + 1)
                if j == -1:
                    raise _ParseError('unclosed quote')
                if self.level + 1 > _MAX_LEX_LEVEL:
                    raise _ParseError('nesting too deep')
                word.subs.append(_Lexer(s[self.i + 1:j], self.level + 1).lex(False))
                word.add(s[self.i:j + 1])
                self.i = j + 1
            elif c == "'" and not in_dq:
                j = s.find("'", self.i + 1)
                if j == -1:
                    raise _ParseError('unclosed quote')
                word.add(s[self.i:j + 1])
                self.i = j + 1
            elif c == '"':
                self.i += 1
                inner = _Word()
                self._dq(inner, False)
                word.subs.extend(inner.subs)
                word.add('"' + inner.text + '"')
            elif c == '}':
                word.add('}')
                self.i += 1
                return
            else:
                word.add(c)
                self.i += 1

    def _dollar(self, word: _Word, in_dq: bool) -> None:
        s, n = self.s, len(self.s)
        nxt = s[self.i + 1] if self.i + 1 < n else ''
        if s.startswith('$((', self.i):
            self._arith(word)
        elif nxt == '(':
            self.i += 2
            word.subs.append(self._nested())
            word.add('$(...)')
        elif nxt == '{':
            self._braced(word, in_dq)
        elif nxt == "'" and not in_dq:
            self.i += 2
            buf = []
            while True:
                if self.i >= n:
                    raise _ParseError('unclosed $quote')
                c = s[self.i]
                if c == '\\' and self.i + 1 < n:
                    buf.append(s[self.i:self.i + 2])
                    self.i += 2
                    continue
                self.i += 1
                if c == "'":
                    break
                buf.append(c)
            word.add(''.join(buf), True)
        else:
            word.add('$')
            self.i += 1

    def _dq(self, word: _Word, in_heredoc: bool) -> None:
        """Scan double-quote rules from self.i; a heredoc body has no closing quote."""
        s, n = self.s, len(self.s)
        escapes = '$`\\' if in_heredoc else '"$`\\'
        while True:
            if self.i >= n:
                if in_heredoc:
                    return
                raise _ParseError('unclosed double quote')
            c = s[self.i]
            if c == '"' and not in_heredoc:
                self.i += 1
                return
            if c == '\\':
                nx = s[self.i + 1] if self.i + 1 < n else ''
                if nx and nx in escapes:
                    word.add(nx, True)
                    self.i += 2
                elif nx == '\n':
                    self.i += 2
                else:
                    word.add('\\', True)
                    self.i += 1
            elif c == '$':
                self._dollar(word, True)
            elif c == '`':
                self._backtick(word)
            else:
                word.add(c, True)
                self.i += 1

    # --- heredoc ---

    def _read_heredocs(self, pending: list) -> None:
        """Take heredoc bodies that start at self.i; a body without a closing line is not one."""
        s, n = self.s, len(self.s)
        for redirect in pending:
            delim = redirect.delim
            if delim is None:
                continue
            strip_tabs = redirect.text == '<<-'
            pos = self.i
            while pos < n:
                eol = s.find('\n', pos)
                line = s[pos:eol if eol != -1 else n]
                if (line.lstrip('\t') if strip_tabs else line) == delim.text:
                    redirect.body = s[self.i:pos]
                    self.i = eol + 1 if eol != -1 else n
                    break
                if eol == -1:
                    break
                pos = eol + 1
            if redirect.body is not None and not delim.quoted:
                if self.level + 1 > _MAX_LEX_LEVEL:
                    raise _ParseError('nesting too deep')
                body_lexer = _Lexer(redirect.body, self.level + 1)
                sink = _Word()
                body_lexer._dq(sink, True)
                redirect.body_subs = sink.subs
        pending.clear()

    # --- main scan ---

    def lex(self, nested: bool) -> list:
        """Tokens from self.i; nested mode stops after the unquoted ')' at depth 0."""
        s, n = self.s, len(self.s)
        toks: list = []
        word: Optional[_Word] = None
        wait: Optional[_Tok] = None
        pending: list = []
        depth = 0

        def flush() -> None:
            nonlocal word, wait
            if word is None:
                return
            tok = _Tok('W', word.text, word.quoted, word.subs)
            if nested and tok.text == 'case' and not tok.quoted:
                raise _ParseError('case inside substitution')
            toks.append(tok)
            if wait is not None:
                wait.delim = tok
                pending.append(wait)
                wait = None
            word = None

        while True:
            if self.i >= n:
                flush()
                if nested:
                    raise _ParseError('unclosed substitution')
                return toks
            c = s[self.i]
            if c == '\n':
                flush()
                wait = None
                toks.append(_Tok('C', '\n'))
                self.i += 1
                self._read_heredocs(pending)
            elif c in ' \t':
                flush()
                self.i += 1
            elif c == '\\':
                nx = s[self.i + 1] if self.i + 1 < n else ''
                if nx == '\n':
                    self.i += 2
                    continue
                if word is None:
                    word = _Word()
                if nx:
                    word.add(nx, True)
                    self.i += 2
                else:
                    word.add('\\', True)
                    self.i += 1
            elif c == "'":
                j = s.find("'", self.i + 1)
                if j == -1:
                    raise _ParseError('unclosed quote')
                if word is None:
                    word = _Word()
                word.add(s[self.i + 1:j], True)
                self.i = j + 1
            elif c == '"':
                if word is None:
                    word = _Word()
                word.quoted = True
                self.i += 1
                self._dq(word, False)
            elif c == '$':
                if word is None:
                    word = _Word()
                self._dollar(word, False)
            elif c == '`':
                if word is None:
                    word = _Word()
                self._backtick(word)
            elif c in '<>' and s.startswith('(', self.i + 1):
                if word is None:
                    word = _Word()
                self.i += 2
                word.subs.append(self._nested())
                word.add(c + '(...)')
            elif c == '#' and word is None:
                j = s.find('\n', self.i)
                self.i = n if j == -1 else j
            elif c in '<>' or s.startswith('&>', self.i):
                if word is not None and not word.quoted and not word.subs and word.text.isdigit():
                    word = None
                else:
                    flush()
                if c == '&':
                    op = '&>>' if s.startswith('&>>', self.i) else '&>'
                else:
                    ops = _REDIRECTS_LT if c == '<' else _REDIRECTS_GT
                    op = next(o for o in ops if s.startswith(o, self.i))
                self.i += len(op)
                tok = _Tok('R', op)
                toks.append(tok)
                if op in ('<<', '<<-'):
                    wait = tok
            elif c == '(' and word is not None:
                start = self.i + 1
                self.i = start
                word.subs.append(self._nested())
                code = _qualifier_code(s[start:self.i - 1])
                if code is not None:
                    word.subs.append(_Lexer(code, self.level + 1).lex(False))
                word.add('(...)')
                flush()
            elif c in ';&|()':
                flush()
                wait = None
                if c == '(':
                    depth += 1
                elif c == ')':
                    if depth == 0 and nested:
                        self.i += 1
                        return toks
                    depth = max(depth - 1, 0)
                if c in ';&|' and s.startswith(c, self.i + 1):
                    op = c + c
                elif c == '|' and s.startswith('&', self.i + 1):
                    op = '|&'
                else:
                    op = c
                self.i += len(op)
                toks.append(_Tok('C', op))
            else:
                if word is None:
                    word = _Word()
                word.add(c)
                self.i += 1


def _qualifier_code(group: str) -> Optional[str]:
    """The code of a zsh glob qualifier 'e:code:' (zsh runs it, bash reports a syntax error)."""
    if len(group) < 2 or group[0] != 'e':
        return None
    delim = group[1]
    quote = ''
    code = []
    for ch in group[2:]:
        if quote:
            if ch == quote:
                quote = ''
        elif ch in '\'"':
            quote = ch
        elif ch == delim:
            break
        code.append(ch)
    return ''.join(code)


class _Command:
    def __init__(self) -> None:
        self.words: List[_Tok] = []
        self.heredocs: List[str] = []
        self.herestrings: List[str] = []
        self.subs: list = []

    def empty(self) -> bool:
        return not (self.words or self.subs or self.heredocs or self.herestrings)


def _split_commands(toks: list) -> List[_Command]:
    commands: List[_Command] = []
    cur = _Command()
    skip_next = False
    herestring_next = False
    for tok in toks:
        if tok.kind == 'C' or (tok.kind == 'W' and not tok.quoted and tok.text in ('{', '}')):
            if not cur.empty():
                commands.append(cur)
            cur = _Command()
            skip_next = herestring_next = False
        elif tok.kind == 'R':
            cur.subs.extend(tok.body_subs)
            if tok.body is not None:
                cur.heredocs.append(tok.body)
            if tok.text == '<<<':
                herestring_next = True
            else:
                skip_next = True
        else:
            cur.subs.extend(tok.subs)
            if skip_next:
                skip_next = False
            elif herestring_next:
                cur.herestrings.append(tok.text)
                herestring_next = False
            else:
                cur.words.append(tok)
    if not cur.empty():
        commands.append(cur)
    return commands


def _strip_reserved(words: List[_Tok]) -> List[str]:
    i = 0
    while i < len(words) and not words[i].quoted and words[i].text in _RESERVED_WORDS:
        is_time = words[i].text == 'time'
        i += 1
        if is_time:
            while i < len(words) and words[i].text == '-p':
                i += 1
    return [w.text for w in words[i:]]


def _skip_prefix(head: str, rest: List[str]) -> Tuple[List[int], Optional[str]]:
    """Indexes in rest where the wrapped command may start; env -S also yields a script.
    An option not known to take a value is read both with and without one."""
    value_options, positional = _PREFIX_COMMANDS[head]
    starts = []
    i = 0
    while i < len(rest):
        w = rest[i]
        if head == 'env' and _ENV_ASSIGNMENT.match(w):
            i += 1
        elif head == 'env' and w in ('-S', '--split-string'):
            return [], rest[i + 1] if i + 1 < len(rest) else ''
        elif head == 'env' and w.startswith('--split-string='):
            return [], w[len('--split-string='):]
        elif head == 'env' and w.startswith('-S') and not w.startswith('--'):
            return [], w[2:]
        elif w == '--':
            i += 1
            break
        elif w.startswith('-') and w != '-':
            if w in value_options:
                i += 2
            else:
                if '=' not in w and i + 1 < len(rest) and not rest[i + 1].startswith('-'):
                    starts.append(i + 2 + positional)
                i += 1
        else:
            break
    starts.append(i + positional)
    return [s for s in dict.fromkeys(starts) if s < len(rest)], None


def _shell_script(rest: List[str]) -> Tuple[Optional[str], bool]:
    """(the -c script or None, whether a script file is given)."""
    c_flag = False
    i = 0
    while i < len(rest):
        w = rest[i]
        if w in ('--', '-'):
            i += 1
            break
        if w in _SHELL_VALUE_OPTIONS:
            i += 2
        elif w[:1] in ('-', '+') and len(w) > 1:
            if not w.startswith('--'):
                c_flag = c_flag or 'c' in w[1:]
                # Bundled -o / -O (as in -oc) each take the next word as their value
                i += sum(1 for ch in w[1:] if ch in 'oO')
            i += 1
        else:
            break
    if c_flag:
        return (rest[i] if i < len(rest) else None), False
    return None, i < len(rest)


def _git_resolve(rest: List[str]) -> Tuple[List[str], Optional[str]]:
    """Words after git's global options with -c aliases expanded; a '!' alias yields its script."""
    aliases = {}
    i = 0
    while i < len(rest) and rest[i].startswith('-'):
        opt = rest[i]
        i += 1
        if opt in _GIT_OPTIONS_WITH_ARG and i < len(rest):
            if opt == '-c':
                m = _GIT_ALIAS_OPTION.match(rest[i])
                if m:
                    aliases[m.group(1).lower()] = m.group(2)
            i += 1
    sub = rest[i:]
    seen = set()
    while sub and sub[0].lower() in aliases and sub[0].lower() not in seen:
        seen.add(sub[0].lower())
        value = aliases[sub[0].lower()]
        if value.startswith('!'):
            return sub, value[1:]
        sub = value.split() + sub[1:]
    return sub, None


def _script_forms(text: str, depth: int, out: list, literal: str) -> None:
    if depth > _MAX_UNWRAP_DEPTH:
        out.append((literal, OPAQUE))
        return
    try:
        collected: list = []
        _process(_Lexer(text).lex(False), depth, collected)
    except (_ParseError, RecursionError):
        collected = [(text, OPAQUE)]
    out.extend(collected)


def _head_and_rest(core: List[str]) -> Tuple[str, List[str]]:
    """Command name (basename, lowercased; zsh '=cmd' and 'git-sub' unfolded) and its args."""
    first = core[0][1:] if core[0].startswith('=') and len(core[0]) > 1 else core[0]
    head = os.path.basename(first).lower()
    rest = core[1:]
    if head.startswith('git-') and len(head) > len('git-'):
        head, rest = 'git', [head[len('git-'):]] + rest
    return head, rest


def _expand(
    words: List[str], heredocs: List[str], herestrings: List[str], depth: int, out: list,
    chain: int = 0,
) -> None:
    literal = ' '.join(words)
    if len(out) >= _MAX_FORMS:
        if not out or out[-1][1] != OPAQUE:
            out.append((literal, OPAQUE))
        return
    i = 0
    while i < len(words) and _ENV_ASSIGNMENT.match(words[i]):
        i += 1
    core = words[i:]
    if not core:
        return
    head, rest = _head_and_rest(core)
    canonical = ' '.join([head] + rest)
    if head in _PREFIX_COMMANDS:
        out.append((literal, canonical))
        if chain >= _MAX_PREFIX_CHAIN:
            out.append((literal, OPAQUE))
            return
        starts, script = _skip_prefix(head, rest)
        if script is not None:
            _script_forms(script, depth + 1, out, literal)
        for start in starts:
            _expand(rest[start:], heredocs, herestrings, depth, out, chain + 1)
        return
    if head in _SHELLS:
        out.append((literal, canonical))
        script, has_file = _shell_script(rest)
        if script is not None:
            _script_forms(script, depth + 1, out, literal)
        elif not has_file:
            for text in heredocs + herestrings:
                _script_forms(text, depth + 1, out, literal)
        return
    if head == 'eval':
        out.append((literal, canonical))
        args = rest[1:] if rest[:1] == ['--'] else rest
        _script_forms(' '.join(args), depth + 1, out, literal)
        return
    if head == 'git':
        sub, script = _git_resolve(rest)
        if script is not None:
            _script_forms(script, depth + 1, out, literal)
        canonical = ' '.join(['git'] + sub)
    out.append((literal, canonical))


def _process(toks: list, depth: int, out: list) -> None:
    for cmd in _split_commands(toks):
        for sub in cmd.subs:
            _process(sub, depth, out)
        _expand(_strip_reserved(cmd.words), cmd.heredocs, cmd.herestrings, depth, out)


def command_forms(command: str) -> List[Tuple[str, str]]:
    """(literal, canonical) for every simple command in command, including nested ones.

    Input the lexer cannot read becomes a single OPAQUE form: guessing at it has hidden commands."""
    out: list = []
    _script_forms(command, 0, out, command)
    return out
