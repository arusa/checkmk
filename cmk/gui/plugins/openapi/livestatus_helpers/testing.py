#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Copyright (C) 2020 tribe29 GmbH - License: GNU General Public License v2
# This file is part of Checkmk (https://checkmk.com). It is subject to the terms and
# conditions defined in the file COPYING, which is part of this source code package.
"""This module collects code which helps with testing Checkmk.

For code to be admitted to this module, it should itself be tested thoroughly, so we won't
have any friction during testing with these helpers themselves.

"""
import contextlib
import datetime as dt
import io
import operator
import os
import re
import time
from typing import (
    Any,
    Callable,
    ContextManager,
    Dict,
    Generator,
    List,
    Literal,
    Optional,
    Tuple,
    Union,
)
# TODO: Make livestatus.py a well tested package on pypi
# TODO: Move this code to the livestatus package
# TODO: Multi-site support. Need to have multiple lists of queries, one per site.
from unittest import mock

from werkzeug.test import EnvironBuilder

from livestatus import MultiSiteConnection

from cmk.gui import http, sites
from cmk.gui.globals import AppContext, RequestContext

FilterFunc = Callable[[Dict[str, Any]], bool]
MatchType = Literal["strict", "ellipsis", "loose"]
OperatorFunc = Callable[[Any, Any], bool]
Response = List[List[Any]]


class FakeSocket:
    def __init__(self, mock_live: 'MockLiveStatusConnection'):
        self.mock_live = mock_live

    def settimeout(self, timeout: Optional[int]):
        pass

    def connect(self, address: str):
        pass

    def recv(self, length: int) -> bytes:
        return self.mock_live.socket_recv(length)

    def send(self, data: bytes):
        return self.mock_live.socket_send(data)


def _make_livestatus_response(response):
    """Build a (somewhat) convincing LiveStatus response

    Special response headers are not honored yet.

    >>> _make_livestatus_response([['foo'], [1, {}]])
    "200          18\\n[['foo'], [1, {}]]"

    >>> _make_livestatus_response([['foo'], [1, {}]])[:16]
    '200          18\\n'

    Args:
        response:
            Some python struct.

    Returns:
        The fake LiveStatus response as a string.

    """
    data = repr(response)
    code = 200
    length = len(data)
    return f"{code:<3} {length:>11}\n{data}"


class MockLiveStatusConnection:
    """Mock a LiveStatus connection.

    NOTE:
        You probably want to use the fixture: cmk.gui.conftest:mock_livestatus

    This object can remember queries and the order in which they should arrive. Once the expected
    query was accepted the query is evaluated and a response is constructed from stored table data.

    It is up to the test-writer to set the appropriate queries and populate the table data.

    The class will verify that the expected queries (and _only_ those) are being issued
    in the `with` block. This means that:
         * Any additional query will trigger a RuntimeError
         * Any missing query will trigger a RuntimeError
         * Any mismatched query will trigger a RuntimeError
         * Any wrong order of queries will trigger a RuntimeError

    Args:
        report_multiple (bool):
            When set to True, this will potentially trigger mutliple Exceptions on __exit__. This
            can be useful when debugging chains of queries. Default is False.

    Examples:

        This test will pass:

            >>> live = (MockLiveStatusConnection()
            ...         .expect_query("GET hosts\\nColumns: name")
            ...         .expect_query("GET services\\nColumns: description"))
            >>> with live(expect_status_query=False):
            ...     live._lookup_next_query("GET hosts\\nColumns: name")  # returns nothing!
            ...     live._lookup_next_query(
            ...         "GET services\\nColumns: description\\nColumnHeaders: on")
            [['heute'], ['example.com']]
            [['description'], ['Memory'], ['CPU load'], ['CPU load']]


        This test will pass as well (useful in real GUI or REST-API calls):

            >>> live = MockLiveStatusConnection()
            >>> with live:
            ...     response = live._lookup_next_query(
            ...         'GET status\\n'
            ...         'Cache: reload\\n'
            ...         'Columns: livestatus_version program_version program_start '
            ...         'num_hosts num_services'
            ...     )
            ...     # Response looks like [['2020-07-03', 'Check_MK 2020-07-03', 1593762478, 1, 36]]
            ...     assert len(response) == 1
            ...     assert len(response[0]) == 5

        This example will fail due to missing queries:

            >>> live = MockLiveStatusConnection()
            >>> with live():  # works either when called or not called
            ...      pass
            Traceback (most recent call last):
            ...
            RuntimeError: Expected queries were not queried:
             * 'GET status\\nCache: reload\\nColumns: livestatus_version program_version \
program_start num_hosts num_services'

        This example will fail due to a wrong query being issued:

            >>> live = MockLiveStatusConnection().expect_query("Hello\\nworld!")
            >>> with live(expect_status_query=False):
            ...     live._lookup_next_query("Foo\\nbar!")
            Traceback (most recent call last):
            ...
            RuntimeError: Expected query (loose):
             * 'Hello\\nworld!'
            Got query:
             * 'Foo\\nbar!'

        This example will fail due to a superfluous query being issued:

            >>> live = MockLiveStatusConnection()
            >>> with live(expect_status_query=False):
            ...     live._lookup_next_query("Spanish inquisition!")
            Traceback (most recent call last):
            ...
            RuntimeError: Got unexpected query:
             * 'Spanish inquisition!'

    """
    def __init__(self, report_multiple: bool = False) -> None:
        self._prepend_site = False
        self._expected_queries: List[Tuple[str, List[str], List[List[str]], MatchType]] = []
        self._num_queries = 0
        self._query_index = 0
        self._report_multiple = report_multiple
        self._expect_status_query: Optional[bool] = None

        self.socket = FakeSocket(self)
        self._last_response: Optional[io.StringIO] = None

        # We store some default values for some tables. May be expanded in the future.

        # Just that parse_check_mk_version is happy we replace the dashes with dots.
        _today = str(dt.datetime.utcnow().date()).replace("-", ".")
        _program_start_timestamp = int(time.time())
        self._tables: Dict[str, List[Dict[str, Any]]] = {
            'status': [{
                'livestatus_version': _today,
                'program_version': f'Check_MK {_today}',
                'program_start': _program_start_timestamp,
                'num_hosts': 1,
                'num_services': 36,
                'helper_usage_cmk': 0.00151953,
                'helper_usage_fetcher': 0.00151953,
                'helper_usage_checker': 0.00151953,
                'helper_usage_generic': 0.00151953,
                'average_latency_cmk': 0.0846039,
                'average_latency_fetcher': 0.0846039,
                'average_latency_generic': 0.0846039,
            }],
            'downtimes': [{
                'id': 54,
                'host_name': 'heute',
                'service_description': 'CPU load',
                'is_service': 1,
                'author': 'cmkadmin',
                'start_time': 1593770319,
                'end_time': 1596448719,
                'recurring': 0,
                'comment': 'Downtime for service',
            }],
            'hosts': [
                {
                    'name': 'heute',
                    'parents': ['example.com'],
                },
                {
                    'name': 'example.com',
                    'parents': [],
                },
            ],
            'services': [
                {
                    'host_name': 'example.com',
                    'description': 'Memory',
                },
                {
                    'host_name': 'example.com',
                    'description': 'CPU load',
                },
                {
                    'host_name': 'heute',
                    'description': 'CPU load',
                },
            ],
            'hostgroups': [
                {
                    'name': 'heute',
                    'members': ['heute'],
                },
                {
                    'name': 'example',
                    'members': ['example.com', 'heute'],
                },
            ],
            'servicegroups': [
                {
                    'name': 'heute',
                    'members': [['heute', 'Memory']],
                },
                {
                    'name': 'example',
                    'members': [
                        ['example.com', 'Memory'],
                        ['example.com', 'CPU load'],
                        ['heute', 'CPU load'],
                    ],
                },
            ],
        }

    def _expect_post_connect_query(self) -> None:
        # cmk.gui.sites._connect_multiple_sites asks for some specifics upon initial connection.
        # We expect this query and give the expected result.
        self.expect_query(
            [
                'GET status',
                'Cache: reload',
                'Columns: livestatus_version program_version program_start num_hosts num_services',
            ],
            force_pos=0,  # first query to be expected
        )

    def __call__(self, expect_status_query=True) -> 'MockLiveStatusConnection':
        self._expect_status_query = expect_status_query
        return self

    def __enter__(self) -> 'MockLiveStatusConnection':
        # This simulates a call to sites.live(). Upon call of sites.live(), the connection will be
        # ensured via _ensure_connected. This sends off a specific query to LiveStatus which we
        # expect to be called as the first query.
        if self._expect_status_query is None:
            self._expect_status_query = True

        if self._expect_status_query:
            self._expect_post_connect_query()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        # We reset this, so the object can be re-used.
        self._expect_status_query = None
        if exc_type and not self._report_multiple:
            # In order not to confuse the programmer too much we skip the other collected
            # exceptions. This skip can be deactivated.
            return
        if self._expected_queries:
            remaining_queries = ""
            for query in self._expected_queries:
                remaining_queries += f"\n * {repr(query[0])}"
            raise RuntimeError(f"Expected queries were not queried:{remaining_queries}")

    def add_table(self, name: str, data: List[Dict[str, Any]]) -> 'MockLiveStatusConnection':
        """Add the data of a table.

        This is desirable in tests, to isolate the individual tests from one another. It is not
        recommended to use the global test-data for all the tests.

        Examples:

            If a table is set, the table is replaced.

                >>> host_list = [{'name': 'heute'}, {'name': 'gestern'}]

                >>> live = MockLiveStatusConnection()
                >>> _ = live.add_table('hosts', host_list)

            The table actually get's replaced, but only for this instance.

                >>> live._tables['hosts'] == host_list
                True

                >>> live = MockLiveStatusConnection()
                >>> live._tables['hosts'] == host_list
                False

        """
        self._tables[name] = data
        return self

    def expect_query(
        self,
        query: Union[str, List[str]],
        match_type: MatchType = 'loose',
        force_pos: Optional[int] = None,
    ) -> 'MockLiveStatusConnection':
        """Add a LiveStatus query to be expected by this class.

        This method is chainable, as it returns the instance again.

        Args:
            query:
                The expected query. May be a `str` or a list of `str` which, in the list case, will
                be joined by newlines.

            match_type:
                Flags with which to decide comparison behavior.
                Can be either 'strict' or 'ellipsis'. In case of 'ellipsis', the supplied query
                can have placeholders in the form of '...'. These placeholders are ignored in the
                comparison.

            force_pos:
                Only used internally. Ignore.

        Returns:
            The object itself, so you can chain.

        Raises:
            KeyError: when a table or a column used by the `query` is not defined in the test-data.

            ValueError: when an unknown `match_type` is given.

        """
        if match_type not in ('strict', 'ellipsis', 'loose'):
            raise ValueError(f"match_type {match_type!r} not supported.")

        if isinstance(query, list):
            query = '\n'.join(query)

        query = query.rstrip("\n")

        table = _table_of_query(query)
        # If the columns are explicitly asked for, we get the columns here.
        columns = _column_of_query(query) or []

        if table and not columns:
            # Otherwise, we figure out the columns from the table store.
            for entry in self._tables.get(table, []):
                columns = sorted(entry.keys())

        # If neither table nor columns can't be deduced, we default to an empty response.
        result = []
        if table and columns:
            # We check the store for data and filter for the actual data that is requested.
            if table not in self._tables:
                raise KeyError(f"Table {table!r} not stored. Call .add_table(...)")
            result_dicts = evaluate_filter(query, self._tables[table])
            for entry in result_dicts:
                row = []
                for col in columns:
                    if col not in entry:
                        raise KeyError(f"Column '{table}.{col}' not known. "
                                       "Add to test-data or fix query.")
                    row.append(entry[col])
                result.append(row)

        if force_pos is not None:
            self._expected_queries.insert(force_pos, (query, columns, result, match_type))
        else:
            self._expected_queries.append((query, columns, result, match_type))

        return self

    def _lookup_next_query(self, query: str) -> Response:
        if self._expect_status_query is None:
            raise RuntimeError("Please use MockLiveStatusConnection as a context manager.")

        header_dict = _unpack_headers(query)
        # NOTE: Cache, Localtime, OutputFormat, KeepAlive, ResponseHeader not yet honored
        show_columns = header_dict.pop('ColumnHeaders', 'off')

        if not self._expected_queries:
            raise RuntimeError(f"Got unexpected query:\n" f" * {repr(query)}")

        expected_query, columns, response, match_type = self._expected_queries[0]

        if not _compare(expected_query, query, match_type):
            raise RuntimeError(f"Expected query ({match_type}):\n"
                               f" * {repr(expected_query)}\n"
                               f"Got query:\n"
                               f" * {repr(query)}")

        # Passed, remove this entry.
        self._expected_queries.pop(0)

        def _generate_output():
            if show_columns == 'on':
                yield columns

            if self._prepend_site:
                yield from [['NO_SITE'] + line for line in response]
            else:
                yield from response

        response = list(_generate_output())
        self._last_response = io.StringIO(_make_livestatus_response(response))
        return response

    def socket_recv(self, length):
        if self._last_response is None:
            raise RuntimeError("Nothing sent yet. Can't receive!")
        return self._last_response.read(length).encode('utf-8')

    def socket_send(self, data: bytes):
        if data[-2:] == b"\n\n":
            data = data[:-2]
        self._lookup_next_query(data.decode('utf-8'))

    def create_socket(self, family):
        return self.socket

    def set_prepend_site(self, prepend_site: bool) -> None:
        self._prepend_site = prepend_site


def _compare(pattern: str, string: str, match_type: MatchType) -> bool:
    """Compare two strings on different ways.

    Examples:
        >>> _compare("asdf", "asdf", "strict")
        True

        >>> _compare("...", "asdf", "ellipsis")
        True

        >>> _compare("...b", "abc", "ellipsis")
        False

        >>> _compare("foo", "asdf", "ellipsis")
        False

        >>> _compare("Hello ... world!", "Hello cruel world!", "ellipsis")
        True

        >>> _compare("COMMAND [...] DEL_FOO;", "COMMAND [123] DEL_FOO;", "ellipsis")
        True

        >>> _compare("GET hosts\\nColumns: name",
        ...          "GET hosts\\nCache: reload\\nColumns: name\\nLocaltime: 12345",
        ...          'loose')
        True

        >>> _compare("GET hosts\\nColumns: name",
        ...          "GET hosts\\nCache: reload\\nColumns: alias name\\nLocaltime: 12345",
        ...          'loose')
        False

    Args:
        pattern:
            The expected string.
            When `match_type` is set to 'ellipsis', may contain '...' for placeholders.

        string:
            The string to compare the pattern with.

        match_type:
            Strict comparisons or with placeholders.

    Returns:
        A boolean, indicating the match.

    """
    if match_type == 'loose':
        # FIXME: Too loose, needs to be more strict.
        #   "GET hosts" also matches "GET hosts\nColumns: ..." which should not be possible.
        string_lines = string.splitlines()
        for line in pattern.splitlines():
            if line.startswith("Cache: "):
                continue
            if line not in string_lines:
                result = False
                break
        else:
            result = True
    elif match_type == 'strict':
        result = pattern == string
    elif match_type == 'ellipsis':
        final_pattern = pattern.replace("[", "\\[").replace("...", ".*?")  # non-greedy match
        result = bool(re.match(f"^{final_pattern}$", string))
    else:
        raise LookupError(f"Unsupported match behaviour: {match_type}")

    return result


@contextlib.contextmanager
def mock_livestatus(with_context=False):
    def enabled_and_disabled_sites(_user):
        return {'NO_SITE': {'socket': 'unix:'}}, {}

    live = MockLiveStatusConnection()

    env = EnvironBuilder().get_environ()
    req = http.Request(env)

    app_context: ContextManager
    req_context: ContextManager
    if with_context:
        app_context = AppContext(None)
        req_context = RequestContext(req=req)
    else:
        app_context = contextlib.nullcontext()
        req_context = contextlib.nullcontext()

    with app_context, req_context, \
         mock.patch("cmk.gui.sites._get_enabled_and_disabled_sites",
                    new=enabled_and_disabled_sites), \
         mock.patch("livestatus.MultiSiteConnection.set_prepend_site",
                    new=live.set_prepend_site), \
         mock.patch("livestatus.MultiSiteConnection.expect_query",
                    new=live.expect_query, create=True), \
         mock.patch("livestatus.SingleSiteConnection._create_socket", new=live.create_socket), \
         mock.patch.dict(os.environ, {'OMD_ROOT': '/', 'OMD_SITE': 'NO_SITE'}):

        yield live


@contextlib.contextmanager
def simple_expect(
    query='',
    match_type: MatchType = "loose",
    expect_status_query=True,
) -> Generator[MultiSiteConnection, None, None]:
    """A simplified testing context manager.

    Args:
        query:
            A livestatus query.

        match_type:
            Either 'strict' or 'ellipsis'. Default is 'ellipsis'.

        expect_status_query:
            If the query of the status table (which Checkmk does when calling sites.live()) should
            be expected. Defaults to False.

    Returns:
        A context manager.

    Examples:

        >>> with simple_expect("GET hosts") as _live:
        ...    _ = _live.query("GET hosts")

    """
    with mock_livestatus(with_context=True) as mock_live:
        if query:
            mock_live.expect_query(query, match_type=match_type)
        with mock_live(expect_status_query=expect_status_query):
            yield sites.live()


def evaluate_filter(query: str, result: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Filter a list of dictionaries according to the filters of a LiveStatus query.

    The filters will be extracted from the query. And: and Or: directives are also supported.

    Currently only standard "Filter:" directives are supported, not StatsFilter: etc.

    Args:
        query:
            A LiveStatus query as a string.

        result:
            A list of dictionaries representing a LiveStatus table. The keys need to match the
            columns of the LiveStatus query. A mismatch will lead to, at least, a KeyError.

    Examples:

        >>> q = "GET hosts\\nFilter: name = heute"
        >>> data = [{'name': 'heute', 'state': 0}, {'name': 'morgen', 'state': 1}]
        >>> evaluate_filter(q, data)
        [{'name': 'heute', 'state': 0}]

        >>> q = "GET hosts\\nFilter: name = heute\\nFilter: state > 0\\nAnd: 2"
        >>> evaluate_filter(q, data)
        []

        >>> q = "GET hosts\\nFilter: name = heute\\nFilter: state = 0\\nAnd: 2"
        >>> evaluate_filter(q, data)
        [{'name': 'heute', 'state': 0}]

        >>> q = "GET hosts\\nFilter: name = heute\\nFilter: state > 0\\nOr: 2"
        >>> evaluate_filter(q, data)
        [{'name': 'heute', 'state': 0}, {'name': 'morgen', 'state': 1}]

        >>> q = "GET hosts\\nFilter: name ~ heu"
        >>> evaluate_filter(q, data)
        [{'name': 'heute', 'state': 0}]

    Returns:
        The filtered list of dictionaries.

    """
    filters = []
    for line in query.splitlines():
        if line.startswith("Filter:"):
            filters.append(make_filter_func(line))
        elif line.startswith(("And:", "Or:")):
            op, count_ = line.split(" ", 1)
            count = int(count_)
            filters, params = filters[:-count], filters[-count:]
            filters.append(COMBINATORS[op](params))

    if not filters:
        # No filtering requested. Dump all the data.
        return result

    if len(filters) > 1:
        raise ValueError(f"Got {len(filters)} filters, expected one. Forgot And/Or?")

    return [entry for entry in result if filters[0](entry)]


def and_(filters: List[FilterFunc]) -> FilterFunc:
    """Combines multiple filters via a logical AND.

    Args:
        filters:
            A list of filter functions.

    Returns:
        True if all filters return True, else False.

    Examples:

        >>> and_([lambda x: True, lambda x: False])({})
        False

        >>> and_([lambda x: True, lambda x: True])({})
        True

    """
    def _and_impl(entry: Dict[str, Any]) -> bool:
        return all([filt(entry) for filt in filters])

    return _and_impl


def or_(filters: List[FilterFunc]) -> FilterFunc:
    """Combine multiple filters via a logical OR.

    Args:
        filters:
            A list of filter functions.

    Returns:
        True if any of the filters returns True, else False

    Examples:

        >>> or_([lambda x: True, lambda x: False])({})
        True

        >>> or_([lambda x: False, lambda x: False])({})
        False

    """
    def _or_impl(entry: Dict[str, Any]) -> bool:
        return any([filt(entry) for filt in filters])

    return _or_impl


COMBINATORS: Dict[str, Callable[[List[FilterFunc]], FilterFunc]] = {
    'And:': and_,
    'Or:': or_,
}
"""A dict of logical combinator helper functions."""


def cast_down(op: OperatorFunc) -> OperatorFunc:
    """Cast the second argument to the type of the first argument, then compare.

    No explicit checking for compatibility is done. You'll get a ValueError or a TypeError
    (depending on the type) if such a cast is not possible.
    """
    def _casting_op(a: Any, b: Any) -> bool:
        t = type(a)
        return op(a, t(b))

    return _casting_op


def match_regexp(string_: str, regexp: str) -> bool:
    """

    Args:
        string_: The string to check.
        regexp: The regexp to use against the string.

    Returns:
        A boolean.

    Examples:

        >>> match_regexp("heute", "heu")
        True

        >>> match_regexp("heute", " heu")
        False

        >>> match_regexp("heute", ".*")
        True

        >>> match_regexp("heute", "morgen")
        False

    """
    return bool(re.match(regexp, string_))


OPERATORS: Dict[str, OperatorFunc] = {
    '=': cast_down(operator.eq),
    '>': cast_down(operator.gt),
    '<': cast_down(operator.lt),
    '>=': cast_down(operator.le),
    '<=': cast_down(operator.ge),
    '~': match_regexp,
}
"""A dict of all implemented comparison operators."""


def make_filter_func(line: str) -> FilterFunc:
    """Make a filter-function from a LiveStatus-query filter row.

    Args:
        line:
            A LiveStatus filter row.

    Returns:
        A function which checks an entry against the filter.

    Examples:

        Check for some concrete values:

            >>> f = make_filter_func("Filter: name = heute")
            >>> f({'name': 'heute'})
            True

            >>> f({'name': 'morgen'})
            False

            >>> f({'name': ' heute '})
            False

        Check for empty values:

            >>> f = make_filter_func("Filter: name = ")
            >>> f({'name': ''})
            True

            >>> f({'name': 'heute'})
            False

        If not implemented, yell:

            >>> f = make_filter_func("Filter: name !! heute")
            Traceback (most recent call last):
            ...
            ValueError: Operator '!!' not implemented. Please check docs or implement.


    """
    field, op, *value = line[7:].split(None, 2)  # strip Filter: as len("Filter:") == 7
    if op not in OPERATORS:
        raise ValueError(f"Operator {op!r} not implemented. Please check docs or implement.")

    # For checking empty values. In this case an empty list.
    if not value:
        value = ['']

    def _apply_op(entry: Dict[str, Any]) -> bool:
        return OPERATORS[op](entry[field], *value)

    return _apply_op


def _column_of_query(query: str) -> Optional[List[str]]:
    """Figure out the queried columns from a LiveStatus query.

    Args:
        query:
            A LiveStatus query as a string.

    Returns:
        A list of column names referenced by the query.

    Examples:

        >>> _column_of_query('GET hosts\\nColumns: name status alias\\nFilter: name = foo')
        ['name', 'status', 'alias']

        >>> _column_of_query('GET hosts\\nFilter: name = foo')

    """
    for line in query.splitlines():
        if line.startswith('Columns:'):
            return line[8:].split()  # len("Columns:") == 8

    return None


def _table_of_query(query: str) -> Optional[str]:
    """Figure out a table from a LiveStatus query.

    Args:
        query:
            A LiveStatus query as a string.

    Returns:
        The table name referenced by the LiveStatus query.

    Examples:

        >>> _table_of_query("GET hosts\\nColumns: name\\nFilter: name = foo")
        'hosts'

        >>> _table_of_query("GET     hosts\\nColumns: name\\nFilter: name = foo")
        'hosts'

        >>> _table_of_query("GET\\n")

    """
    lines = query.splitlines()
    if lines and lines[0].startswith("GET "):
        return lines[0].split(None, 1)[1]

    return None


def _unpack_headers(query: str) -> Dict[str, str]:
    r"""Unpack and normalize headers from a string.

    Examples:

        >>> _unpack_headers("GET hosts\nColumnHeaders: off")
        {'ColumnHeaders': 'off'}

        >>> _unpack_headers("ColumnHeaders: off\nResponseFormat: fixed16")
        {'ColumnHeaders': 'off', 'ResponseFormat': 'fixed16'}

        >>> _unpack_headers("Foobar!")
        {}

    Args:
        query:
            Query as a string.

    Returns:
        Headers of query as a dict.

    """
    unpacked = {}
    for header in query.splitlines():
        if header.startswith('GET '):
            continue
        if ":" not in header:
            continue
        if not header:
            continue
        key, value = header.split(": ", 1)
        unpacked[key] = value.lstrip(" ")
    return unpacked
