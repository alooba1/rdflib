#!/usr/bin/env python
"""
notation3.py - Standalone Notation3 Parser
Derived from CWM, the Closed World Machine
Authors of the original suite:
* Dan Connolly <@@>
* Tim Berners-Lee <@@>
* Yosi Scharf <@@>
* Joseph M. Reagle Jr. <reagle@w3.org>
* Rich Salz <rsalz@zolera.com>
http://www.w3.org/2000/10/swap/notation3.py
Copyright 2000-2007, World Wide Web Consortium.
Copyright 2001, MIT.
Copyright 2001, Zolera Systems Inc.
License: W3C Software License
http://www.w3.org/Consortium/Legal/copyright-software
Modified by Sean B. Palmer
Copyright 2007, Sean B. Palmer.
Modified to work with rdflib by Gunnar Aastrand Grimnes
Copyright 2010, Gunnar A. Grimnes
"""
import codecs
import os
import re
import sys
import rdflib

# importing typing for `typing.List` because `List`` is used for something else
import typing
from decimal import Decimal
from typing import IO, TYPE_CHECKING, Any, Callable, Dict, Optional, TypeVar, Union
from uuid import uuid4

from rdflib.compat import long_type
from rdflib.exceptions import ParserError
from rdflib.graph import ConjunctiveGraph, Graph, QuotedGraph
from rdflib.term import (
    _XSD_PFX,
    RdfstarTriple,
    BNode,
    Identifier,
    Literal,
    Node,
    URIRef,
    Variable,
    _unique_id,
)

__all__ = [
    "BadSyntax",
    "N3Parser",
    "NtriplesStarParser",
    "splitFragP",
    "join",
    "base",
    "runNamespace",
    "uniqueURI",
    "hexify",
]

from rdflib.parser import Parser

if TYPE_CHECKING:
    from rdflib.parser import InputSource

AnyT = TypeVar("AnyT")


def splitFragP(uriref, punct=0):
    """split a URI reference before the fragment
    Punctuation is kept.
    e.g.
    >>> splitFragP("abc#def")
    ('abc', '#def')
    >>> splitFragP("abcdef")
    ('abcdef', '')
    """

    i = uriref.rfind("#")
    if i >= 0:
        return uriref[:i], uriref[i:]
    else:
        return uriref, ""


def join(here, there):
    """join an absolute URI and URI reference
    (non-ascii characters are supported/doctested;
    haven't checked the details of the IRI spec though)
    ``here`` is assumed to be absolute.
    ``there`` is URI reference.
    >>> join('http://example/x/y/z', '../abc')
    'http://example/x/abc'
    Raise ValueError if there uses relative path
    syntax but here has no hierarchical path.
    >>> join('mid:foo@example', '../foo') # doctest: +NORMALIZE_WHITESPACE
    Traceback (most recent call last):
        raise ValueError(here)
    ValueError: Base <mid:foo@example> has no slash
    after colon - with relative '../foo'.
    >>> join('http://example/x/y/z', '')
    'http://example/x/y/z'
    >>> join('mid:foo@example', '#foo')
    'mid:foo@example#foo'
    We grok IRIs
    >>> len(u'Andr\\xe9')
    5
    >>> join('http://example.org/', u'#Andr\\xe9')
    u'http://example.org/#Andr\\xe9'
    """

    #    assert(here.find("#") < 0), \
    #        "Base may not contain hash: '%s'" % here  # why must caller splitFrag?

    slashl = there.find("/")
    colonl = there.find(":")

    # join(base, 'foo:/') -- absolute
    if colonl >= 0 and (slashl < 0 or colonl < slashl):
        return there

    bcolonl = here.find(":")
    assert bcolonl >= 0, (
        "Base uri '%s' is not absolute" % here
    )  # else it's not absolute

    path, frag = splitFragP(there)
    if not path:
        return here + frag

    # join('mid:foo@example', '../foo') bzzt
    if here[bcolonl + 1 : bcolonl + 2] != "/":
        raise ValueError(
            "Base <%s> has no slash after "
            "colon - with relative '%s'." % (here, there)
        )

    if here[bcolonl + 1 : bcolonl + 3] == "//":
        bpath = here.find("/", bcolonl + 3)
    else:
        bpath = bcolonl + 1

    # join('http://xyz', 'foo')
    if bpath < 0:
        bpath = len(here)
        here = here + "/"

    # join('http://xyz/', '//abc') => 'http://abc'
    if there[:2] == "//":
        return here[: bcolonl + 1] + there

    # join('http://xyz/', '/abc') => 'http://xyz/abc'
    if there[:1] == "/":
        return here[:bpath] + there

    slashr = here.rfind("/")

    while 1:
        if path[:2] == "./":
            path = path[2:]
        if path == ".":
            path = ""
        elif path[:3] == "../" or path == "..":
            path = path[3:]
            i = here.rfind("/", bpath, slashr)
            if i >= 0:
                here = here[: i + 1]
                slashr = i
        else:
            break

    return here[: slashr + 1] + path + frag


def base():
    """The base URI for this process - the Web equiv of cwd
    Relative or absolute unix-standard filenames parsed relative to
    this yield the URI of the file.
    If we had a reliable way of getting a computer name,
    we should put it in the hostname just to prevent ambiguity
    """
    # return "file://" + hostname + os.getcwd() + "/"
    return "file://" + _fixslash(os.getcwd()) + "/"


def _fixslash(s):
    """Fix windowslike filename to unixlike - (#ifdef WINDOWS)"""
    s = s.replace("\\", "/")
    if s[0] != "/" and s[1] == ":":
        s = s[2:]  # @@@ Hack when drive letter present
    return s


CONTEXT = 0
PRED = 1
SUBJ = 2
OBJ = 3

PARTS = PRED, SUBJ, OBJ
ALL4 = CONTEXT, PRED, SUBJ, OBJ

SYMBOL = 0
FORMULA = 1
LITERAL = 2
LITERAL_DT = 21
LITERAL_LANG = 22
ANONYMOUS = 3
XMLLITERAL = 25

Logic_NS = "http://www.w3.org/2000/10/swap/log#"
NODE_MERGE_URI = Logic_NS + "is"  # Pseudo-property indicating node merging
forSomeSym = Logic_NS + "forSome"
forAllSym = Logic_NS + "forAll"

RDF_type_URI = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"
RDF_NS_URI = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
OWL_NS = "http://www.w3.org/2002/07/owl#"
DAML_sameAs_URI = OWL_NS + "sameAs"
parsesTo_URI = Logic_NS + "parsesTo"
RDF_spec = "http://www.w3.org/TR/REC-rdf-syntax/"

List_NS = RDF_NS_URI  # From 20030808
_Old_Logic_NS = "http://www.w3.org/2000/10/swap/log.n3#"

N3_first = (SYMBOL, List_NS + "first")
N3_rest = (SYMBOL, List_NS + "rest")
N3_li = (SYMBOL, List_NS + "li")
N3_nil = (SYMBOL, List_NS + "nil")
N3_List = (SYMBOL, List_NS + "List")
N3_Empty = (SYMBOL, List_NS + "Empty")


runNamespaceValue = None


def runNamespace():
    """Returns a URI suitable as a namespace for run-local objects"""
    # @@@ include hostname (privacy?) (hash it?)
    global runNamespaceValue
    if runNamespaceValue is None:
        runNamespaceValue = join(base(), _unique_id()) + "#"
    return runNamespaceValue


nextu = 0

import re
import lark
import hashlib
from lark import (
    Lark,
    Transformer,
    Tree,
)
from lark.visitors import Visitor
from lark.reconstruct import Reconstructor

from lark.lexer import (
    Token,
)

# from pymantic.compat import (
#     binary_type,
# )
# from pymantic.parsers.base import (
#     BaseParser,
# )
# from pymantic.primitives import (
#     BlankNode,
#     Literal,
#     NamedNode,
#     Triple,
# )
# from pymantic.util import (
#     grouper,
#     smart_urljoin,
#     decode_literal,
# )

grammar = r"""ntriplesdoc: statement*
?statement: directive | triples "." | quotedtriples "."
directive: prefix_id | base | sparql_prefix | sparql_base
prefix_id: "@prefix" PNAME_NS IRIREF "."
base: BASE_DIRECTIVE IRIREF "."
sparql_base: /BASE/i IRIREF
sparql_prefix: /PREFIX/i PNAME_NS IRIREF
triples: subject predicate_object_list
       | blank_node_property_list predicate_object_list?
insidequotation: qtsubject verb qtobject
quotedtriples: triples compoundanno
predicate_object_list: verb object_list (";" (verb object_list)?)*
?object_list: object ("," object)*
?verb: predicate | /a/
?subject: iri | blank_node | collection | quotation
?predicate: iri
?object: iri | blank_node | collection | blank_node_property_list | literal | quotation
?literal: rdf_literal | numeric_literal | boolean_literal
?qtsubject: iri | blank_node | quotation
?qtobject: 	iri | blank_node | literal | quotation
quotation: "<<" insidequotation ">>"
COMPOUNDL: "{|"
COMPOUNDR: "|}"
compoundanno: COMPOUNDL predicate_object_list COMPOUNDR
blank_node_property_list: "[" predicate_object_list "]"
collection: "(" object* ")"
numeric_literal: INTEGER | DECIMAL | DOUBLE
rdf_literal: string (LANGTAG | "^^" iri)?
boolean_literal: /true|false/
string: STRING_LITERAL_QUOTE
      | STRING_LITERAL_SINGLE_QUOTE
      | STRING_LITERAL_LONG_SINGLE_QUOTE
      | STRING_LITERAL_LONG_QUOTE
iri: IRIREF | prefixed_name
prefixed_name: PNAME_LN | PNAME_NS
blank_node: BLANK_NODE_LABEL | ANON
BASE_DIRECTIVE: "@base"
IRIREF: "<" (/[^\x00-\x20<>"{}|^`\\]/ | UCHAR)* ">"
PNAME_NS: PN_PREFIX? ":"
PNAME_LN: PNAME_NS PN_LOCAL
BLANK_NODE_LABEL: "_:" (PN_CHARS_U | /[0-9]/) ((PN_CHARS | ".")* PN_CHARS)?
LANGTAG: "@" /[a-zA-Z]+/ ("-" /[a-zA-Z0-9]+/)*
INTEGER: /[+-]?[0-9]+/
DECIMAL: /[+-]?[0-9]*/ "." /[0-9]+/
DOUBLE: /[+-]?/ (/[0-9]+/ "." /[0-9]*/ EXPONENT
      | "." /[0-9]+/ EXPONENT | /[0-9]+/ EXPONENT)
EXPONENT: /[eE][+-]?[0-9]+/
STRING_LITERAL_QUOTE: "\"" (/[^\x22\x5C\x0A\x0D]/ | ECHAR | UCHAR)* "\""
STRING_LITERAL_SINGLE_QUOTE: "'" (/[^\x27\x5C\x0A\x0D]/ | ECHAR | UCHAR)* "'"
STRING_LITERAL_LONG_SINGLE_QUOTE: "'''" (/'|''/? (/[^'\\]/ | ECHAR | UCHAR))* "'''"
STRING_LITERAL_LONG_QUOTE: "\"\"\"" (/"|""/? (/[^"\\]/ | ECHAR | UCHAR))* "\"\"\""
UCHAR: "\\u" HEX~4 | "\\U" HEX~8
ECHAR: "\\" /[tbnrf"'\\]/
WS: /[\x20\x09\x0D\x0A]/
ANON: "[" WS* "]"
PN_CHARS_BASE: /[A-Za-z\u00C0-\u00D6\u00D8-\u00F6\u00F8-\u02FF\u0370-\u037D\u037F-\u1FFF\u200C-\u200D\u2070-\u218F\u2C00-\u2FEF\u3001-\uD7FF\uF900-\uFDCF\uFDF0-\uFFFD\U00010000-\U000EFFFF]/
PN_CHARS_U: PN_CHARS_BASE | "_"
PN_CHARS: PN_CHARS_U | /[\-0-9\u00B7\u0300-\u036F\u203F-\u2040]/
PN_PREFIX: PN_CHARS_BASE ((PN_CHARS | ".")* PN_CHARS)?
PN_LOCAL: (PN_CHARS_U | ":" | /[0-9]/ | PLX) ((PN_CHARS | "." | ":" | PLX)* (PN_CHARS | ":" | PLX))?
PLX: PERCENT | PN_LOCAL_ESC
PERCENT: "%" HEX~2
HEX: /[0-9A-Fa-f]/
PN_LOCAL_ESC: "\\" /[_~\.\-!$&'()*+,;=\/?#@%]/
%ignore WS
COMMENT: "#" /[^\n]/*
%ignore COMMENT
"""

# r"""ntriplesdoc: statement*
# ?statement: triples "." | triples "."? (EOL triples ".")* EOL?
# triples: subject predicate object
# ?verb: predicate | /a/
# ?subject: IRIREF | BLANK_NODE_LABEL | quotation
# ?predicate: IRIREF
# ?object: IRIREF | BLANK_NODE_LABEL | literal | quotation
# ?literal: rdf_literal | numeric_literal | boolean_literal
# ANGLEBRACKETL: "<<"
# ANGLEBRACKETR: ">>"
# quotation: ANGLEBRACKETL triples ANGLEBRACKETR
# numeric_literal: INTEGER | DECIMAL | DOUBLE
# rdf_literal: string (LANGTAG | "^^" iri)?
# boolean_literal: /true|false/
# string: STRING_LITERAL_QUOTE
#       | STRING_LITERAL_SINGLE_QUOTE
#       | STRING_LITERAL_LONG_SINGLE_QUOTE
#       | STRING_LITERAL_LONG_QUOTE
# iri: IRIREF | prefixed_name
# prefixed_name: PNAME_LN | PNAME_NS

# EOL: /[#xD#xA]+/
# IRIREF: "<" (/[^\x00-\x20<>"{}|^`\\]/ | UCHAR)* ">"
# PNAME_NS: PN_PREFIX? ":"
# PNAME_LN: PNAME_NS PN_LOCAL
# BLANK_NODE_LABEL: "_:" (PN_CHARS_U | /[0-9]/) ((PN_CHARS | ".")* PN_CHARS)?
# LANGTAG: "@" /[a-zA-Z]+/ ("-" /[a-zA-Z0-9]+/)*
# INTEGER: /[+-]?[0-9]+/
# DECIMAL: /[+-]?[0-9]*/ "." /[0-9]+/
# DOUBLE: /[+-]?/ (/[0-9]+/ "." /[0-9]*/ EXPONENT
#       | "." /[0-9]+/ EXPONENT | /[0-9]+/ EXPONENT)
# EXPONENT: /[eE][+-]?[0-9]+/
# STRING_LITERAL_QUOTE: "\"" (/[^\x22\x5C\x0A\x0D]/ | ECHAR | UCHAR)* "\""
# STRING_LITERAL_SINGLE_QUOTE: "'" (/[^\x27\x5C\x0A\x0D]/ | ECHAR | UCHAR)* "'"
# STRING_LITERAL_LONG_SINGLE_QUOTE: "'''" (/'|''/? (/[^'\\]/ | ECHAR | UCHAR))* "'''"
# STRING_LITERAL_LONG_QUOTE: "\"\"\"" (/"|""/? (/[^"\\]/ | ECHAR | UCHAR))* "\"\"\""
# UCHAR: "\\u" HEX~4 | "\\U" HEX~8
# ECHAR: "\\" /[tbnrf"'\\]/
# WS: /[\x20\x09\x0D\x0A]/
# ANON: "[" WS* "]"
# PN_CHARS_BASE: /[A-Za-z\u00C0-\u00D6\u00D8-\u00F6\u00F8-\u02FF\u0370-\u037D\u037F-\u1FFF\u200C-\u200D\u2070-\u218F\u2C00-\u2FEF\u3001-\uD7FF\uF900-\uFDCF\uFDF0-\uFFFD\U00010000-\U000EFFFF]/
# PN_CHARS_U: PN_CHARS_BASE | "_"
# PN_CHARS: PN_CHARS_U | /[\-0-9\u00B7\u0300-\u036F\u203F-\u2040]/
# PN_PREFIX: PN_CHARS_BASE ((PN_CHARS | ".")* PN_CHARS)?
# PN_LOCAL: (PN_CHARS_U | ":" | /[0-9]/ | PLX) ((PN_CHARS | "." | ":" | PLX)* (PN_CHARS | ":" | PLX))?
# PLX: PERCENT | PN_LOCAL_ESC
# PERCENT: "%" HEX~2
# HEX: /[0-9A-Fa-f]/
# PN_LOCAL_ESC: "\\" /[_~\.\-!$&'()*+,;=\/?#@%]/

# %ignore WS
# COMMENT: "#" /[^\n]/*
# %ignore COMMENT
# """

ntriples_lark = Lark(grammar, start="ntriplesdoc", parser="lalr", maybe_placeholders=False)

from lark import Visitor, v_args
quotation_list = []
quotation_dict = dict()
vblist = []
quotationreif = []
prefix_list = []
quotationannolist = []
constructors = ""
quoted_or_not = False
both_quoted_and_asserted = False
assertedtriplelist = []

def myHash(text:str):
  return str(hashlib.md5(text.encode('utf-8')).hexdigest())

class FindVariables(Visitor):
    def __init__(self):
        super().__init__()
        self.variable_list = []

    def quotation(self, var):

        qut = Reconstructor(ntriples_lark).reconstruct(var)
        qut = qut.replace(";", "")
        if not (qut in quotation_list):
            quotation_list.append(qut)

        vr = Reconstructor(ntriples_lark).reconstruct(var)
        vr = vr.replace(";","")
        quotation_dict[qut] = str(myHash(qut)) + "RdfstarTriple"
        qut_hash = ":" + str(myHash(qut))
        id = quotation_dict.get(vr)
        for x in quotation_dict:
            if x in vr:
                vr = vr.replace(x, ":"+quotation_dict.get(x))
                vr = vr.replace("<<", "")
                vr = vr.replace(">>", "")
                output = vr.split(":")
                output.pop(0)
                oa1 = Reconstructor(ntriples_lark).reconstruct(var)
                oa1 = oa1.replace(";","")
                output.append(oa1)
                if (not (output in quotationreif)):
                    quotationreif.append(output)

    def insidequotation(self, var):
        appends1 = []
        for x in var.children:
            x1 = Reconstructor(ntriples_lark).reconstruct(x)
            x1 = x1.replace(";","")
            appends1.append(x1)

        if not (appends1 in vblist):
            vblist.append(appends1)

    def triples(self, var):

        appends1 = []
        tri = Reconstructor(ntriples_lark).reconstruct(var)
        tri = tri.replace(";", "")
        if not (tri in assertedtriplelist):
            assertedtriplelist.append(tri)
        for x in var.children:
            if x.data == 'predicate_object_list':
                xc = x.children
                for y in xc:
                    x2 = Reconstructor(ntriples_lark).reconstruct(y)
                    x2 = x2.replace(";","")
                    appends1.append(x2)
            else:
              anyquotationin = False
              x1 = Reconstructor(ntriples_lark).reconstruct(x)
              x1 = x1.replace(";","")
              appends1.append(x1)

        if not (appends1 in vblist):
            vblist.append(appends1)

def RDFstarParsings(rdfstarstring):
    global quotationannolist, quotation_dict, vblist, quotationreif, prefix_list, constructors, assertedtriplelist, quoted_or_not, both_quoted_and_asserted
    quotationannolist = []
    vblist = []
    quotationreif = []
    prefix_list = []
    constructors = ""
    quoted_or_not = False
    both_quoted_and_asserted = False
    tree = ntriples_lark.parse(rdfstarstring)
    at = FindVariables().visit(tree)

    for y in vblist:
        for element_index in range(0, len(y)):
            if (y[element_index][0] == "_") & (not (element_index == 0)):
                y[element_index]=" "+y[element_index]
        result = "".join(y)
        result = result.replace(" ", "")
        if result in assertedtriplelist:
            test1 = "<<"+result+">>"
            if test1 in quotation_list:
                both_quoted_and_asserted = True
            else:
                both_quoted_and_asserted = False
                quoted_or_not = False
        else:
            test2 = "<<"+result+">>"
            if test2 in quotation_list:
                both_quoted_and_asserted = False
                quoted_or_not = True
            else:
                both_quoted_and_asserted = False
                quoted_or_not = False
        result = "<<"+result+">>"
        if not (result in quotation_list):
            for z in range(0,len(y)):
                if "<<" in y[z]:
                    y[z] = "_:"+quotation_dict[y[z]]
            myvalue = str(myHash(result))
            subject = y[0]
            predicate = y[1]
            object = y[2]
            if both_quoted_and_asserted:
                next_rdf_object = "_:" + str(myvalue) +"RdfstarTriple"+ '\n' + "    a rdfstar:AssertedStatement, rdfstar:QuotedStatement ;\n"+"    rdf:subject "+subject+' ;\n'+"    rdf:predicate "+predicate+" ;\n"+"    rdf:object "+object+" ;\n"+".\n"
            else:
                if quoted_or_not:
                    next_rdf_object = "_:" + str(myvalue) +"RdfstarTriple"+ '\n' + "    a rdfstar:QuotedStatement ;\n"+"    rdf:subject "+subject+' ;\n'+"    rdf:predicate "+predicate+" ;\n"+"    rdf:object "+object+" ;\n"+".\n"
                else:
                    next_rdf_object = "_:" + str(myvalue) +"RdfstarTriple"+ '\n' + "    a rdfstar:AssertedStatement ;\n"+"    rdf:subject "+subject+' ;\n'+"    rdf:predicate "+predicate+" ;\n"+"    rdf:object "+object+" ;\n"+".\n"
            constructors+=next_rdf_object
        else:
            value = quotation_dict[result]
            for z in range(0,len(y)):
                if "<<" in y[z]:
                    y[z] = "_:"+quotation_dict[y[z]]
            subject = y[0]
            predicate = y[1]
            object = y[2]
            if both_quoted_and_asserted:
                next_rdf_object = "_:" + str(value) + '\n' + "    a rdfstar:AssertedStatement, rdfstar:QuotedStatement ;\n"+"    rdf:subject "+subject+' ;\n'+"    rdf:predicate "+predicate+" ;\n"+"    rdf:object "+object+" ;\n"+".\n"
            else:
                if quoted_or_not:
                    next_rdf_object = "_:" + str(value) + '\n' + "    a rdfstar:QuotedStatement ;\n"+"    rdf:subject "+subject+' ;\n'+"    rdf:predicate "+predicate+" ;\n"+"    rdf:object "+object+" ;\n"+".\n"
                else:
                    next_rdf_object = "_:" + str(value) + '\n' + "    a rdfstar:AssertedStatement ;\n"+"    rdf:subject "+subject+' ;\n'+"    rdf:predicate "+predicate+" ;\n"+"    rdf:object "+object+" ;\n"+".\n"
            constructors+=next_rdf_object

    for z in quotationannolist:
        result1 = "".join(z)
        result1 = "<<"+result1+">>"
        if result1 in quotation_list:
            both_quoted_and_asserted = True
        else:
            both_quoted_and_asserted = False
            quoted_or_not = False
        value = str(myHash(result1))
        subject = z[0]
        predicate = z[1]
        object = z[2]
        if both_quoted_and_asserted:
            next_rdf_object = "_:" + str(value) +"RdfstarTriple"+ '\n' + "    a rdfstar:AssertedStatement, rdfstar:QuotedStatement ;\n"+"    rdf:subject "+subject+' ;\n'+"    rdf:predicate "+predicate+" ;\n"+"    rdf:object "+object+" ;\n"+".\n"
        else:
            if quoted_or_not:
                next_rdf_object = "_:" + str(value) +"RdfstarTriple"+ '\n' + "    a rdfstar:QuotedStatement ;\n"+"    rdf:subject "+subject+' ;\n'+"    rdf:predicate "+predicate+" ;\n"+"    rdf:object "+object+" ;\n"+".\n"
            else:
                next_rdf_object = "_:" + str(value) +"RdfstarTriple"+ '\n' + "    a rdfstar:AssertedStatement ;\n"+"    rdf:subject "+subject+' ;\n'+"    rdf:predicate "+predicate+" ;\n"+"    rdf:object "+object+" ;\n"+".\n"
        constructors+=next_rdf_object

    for x in range(0, len(prefix_list)):
        prefix_list[x] = Reconstructor(ntriples_lark).reconstruct(prefix_list[x])
        constructors = prefix_list[x]+"\n"+constructors

    constructors = "PREFIX rdfstar: <https://w3id.org/rdf-star/> \n"+constructors

    constructors = "PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#> \n"+constructors

    if not (("PREFIX : <http://example/>" in constructors) or ("PREFIX:<http://example/>" in constructors)):
        constructors = "PREFIX : <http://example/> \n"+constructors

    if "PREFIX:" in constructors:
        constructors = constructors.replace("PREFIX:", "PREFIX :")

    print("yes?", constructors)
    constructors = bytes(constructors, 'utf-8')
    return constructors

def uniqueURI():
    """A unique URI"""
    global nextu
    nextu += 1
    return runNamespace() + "u_" + str(nextu)

tracking = False
chatty_flag = 50

# from why import BecauseOfData, becauseSubexpression

def BecauseOfData(*args, **kargs):
    # print args, kargs
    pass


def becauseSubexpression(*args, **kargs):
    # print args, kargs
    pass


N3_forSome_URI = forSomeSym
N3_forAll_URI = forAllSym

# Magic resources we know about

ADDED_HASH = "#"  # Stop where we use this in case we want to remove it!
# This is the hash on namespace URIs

RDF_type = (SYMBOL, RDF_type_URI)
DAML_sameAs = (SYMBOL, DAML_sameAs_URI)

LOG_implies_URI = "http://www.w3.org/2000/10/swap/log#implies"

BOOLEAN_DATATYPE = _XSD_PFX + "boolean"
DECIMAL_DATATYPE = _XSD_PFX + "decimal"
DOUBLE_DATATYPE = _XSD_PFX + "double"
FLOAT_DATATYPE = _XSD_PFX + "float"
INTEGER_DATATYPE = _XSD_PFX + "integer"

option_noregen = 0  # If set, do not regenerate genids on output

# @@ I18n - the notname chars need extending for well known unicode non-text
# characters. The XML spec switched to assuming unknown things were name
# characters.
# _namechars = string.lowercase + string.uppercase + string.digits + '_-'
_notQNameChars = set("\t\r\n !\"#$&'()*,+/;<=>?@[\\]^`{|}~")  # else valid qname :-/
_notKeywordsChars = _notQNameChars | {"."}
_notNameChars = _notQNameChars | {":"}  # Assume anything else valid name :-/
_rdfns = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"

hexChars = set("ABCDEFabcdef0123456789")
escapeChars = set("(_~.-!$&'()*+,;=/?#@%)")  # valid for \ escapes in localnames
numberChars = set("0123456789-")
numberCharsPlus = numberChars | {"+", "."}


def unicodeExpand(m):
    try:
        return chr(int(m.group(1), 16))
    except:
        raise Exception("Invalid unicode code point: " + m.group(1))


unicodeEscape4 = re.compile(r"\\u([0-9a-fA-F]{4})")
unicodeEscape8 = re.compile(r"\\U([0-9a-fA-F]{8})")


N3CommentCharacter = "#"  # For unix script  # ! compatibility

# Parse string to sink
#
# Regular expressions:
eol = re.compile(r"[ \t]*(#[^\n]*)?\r?\n")  # end  of line, poss. w/comment
eof = re.compile(r"[ \t]*(#[^\n]*)?$")  # end  of file, poss. w/comment
ws = re.compile(r"[ \t]*")  # Whitespace not including NL
signed_integer = re.compile(r"[-+]?[0-9]+")  # integer
integer_syntax = re.compile(r"[-+]?[0-9]+")
decimal_syntax = re.compile(r"[-+]?[0-9]*\.[0-9]+")
exponent_syntax = re.compile(
    r"[-+]?(?:[0-9]+\.[0-9]*|\.[0-9]+|[0-9]+)(?:e|E)[-+]?[0-9]+"
)
digitstring = re.compile(r"[0-9]+")  # Unsigned integer
interesting = re.compile(r"""[\\\r\n\"\']""")
langcode = re.compile(r"[a-zA-Z0-9]+(-[a-zA-Z0-9]+)*")

quoted_triple_list = []
class SinkParser:
    def __init__(
        self,
        store: "RDFSink",
        openFormula: Optional["Formula"] = None,
        thisDoc: str = "",
        baseURI: Optional[str] = None,
        genPrefix: str = "",
        why: Optional[Callable[[], None]] = None,
        turtle: bool = False,
    ):
        """note: namespace names should *not* end in  # ;
        the  # will get added during qname processing"""

        self._bindings = {}
        if thisDoc != "":
            assert ":" in thisDoc, "Document URI not absolute: <%s>" % thisDoc
            self._bindings[""] = thisDoc + "#"  # default

        self._store = store
        if genPrefix:
            # TODO FIXME: there is no function named setGenPrefix
            store.setGenPrefix(genPrefix)  # type: ignore[attr-defined] # pass it on

        self._thisDoc = thisDoc
        self.lines = 0  # for error handling
        self.startOfLine = 0  # For calculating character number
        self._genPrefix = genPrefix
        self.keywords = ["a", "this", "bind", "has", "is", "of", "true", "false"]
        self.keywordsSet = 0  # Then only can others be considered qnames
        self._anonymousNodes: Dict[str, Node] = {}
        self._rdfstartripleNodes: Dict[str, Node] = {}
        # Dict of anon nodes already declared ln: Term
        self._variables: Dict[Identifier, Identifier] = {}
        self._parentVariables: Dict[Identifier, Identifier] = {}
        self._reason = why  # Why the parser was asked to parse this

        self.turtle = turtle  # raise exception when encountering N3 extensions
        # Turtle allows single or double quotes around strings, whereas N3
        # only allows double quotes.
        self.string_delimiters = ('"', "'") if turtle else ('"',)

        self._reason2 = None  # Why these triples
        # was: diag.tracking
        if tracking:
            self._reason2 = BecauseOfData(
                store.newSymbol(thisDoc), because=self._reason
            )

        self._baseURI: Optional[str]
        if baseURI:
            self._baseURI = baseURI
        else:
            if thisDoc:
                self._baseURI = thisDoc
            else:
                self._baseURI = None

        assert not self._baseURI or ":" in self._baseURI

        if not self._genPrefix:
            if self._thisDoc:
                self._genPrefix = self._thisDoc + "#_g"
            else:
                self._genPrefix = uniqueURI()

        self._formula: Formula
        if openFormula is None and not turtle:
            if self._thisDoc:
                # TODO FIXME: store.newFormula does not take any arguments
                self._formula = store.newFormula(thisDoc + "#_formula")  # type: ignore[call-arg]
            else:
                self._formula = store.newFormula()
        else:
            self._formula = openFormula  # type: ignore[assignment]

        self._context = self._formula
        self._parentContext: Optional[Formula] = None

    def here(self, i: int) -> str:
        """String generated from position in file
        This is for repeatability when referring people to bnodes in a document.
        This has diagnostic uses less formally, as it should point one to which
        bnode the arbitrary identifier actually is. It gives the
        line and character number of the '[' charcacter or path character
        which introduced the blank node. The first blank node is boringly
        _L1C1. It used to be used only for tracking, but for tests in general
        it makes the canonical ordering of bnodes repeatable."""

        return "%s_L%iC%i" % (self._genPrefix, self.lines, i - self.startOfLine + 1)

    def formula(self):
        return self._formula

    def loadStream(self, stream: Union[IO[str], IO[bytes]]) -> Optional["Formula"]:
        return self.loadBuf(stream.read())  # Not ideal

    def loadBuf(self, buf: Union[str, bytes]):
        """Parses a buffer and returns its top level formula"""
        self.startDoc()

        self.feed(buf)
        return self.endDoc()  # self._formula

    def feed(self, octets: Union[str, bytes]):
        """Feed an octet stream to the parser
        if BadSyntax is raised, the string
        passed in the exception object is the
        remainder after any statements have been parsed.
        So if there is more data to feed to the
        parser, it should be straightforward to recover."""

        if not isinstance(octets, str):
            s = octets.decode("utf-8")
            # NB already decoded, so \ufeff
            if len(s) > 0 and s[0] == codecs.BOM_UTF8.decode("utf-8"):
                s = s[1:]
        else:
            s = octets

        i = 0
        while i >= 0:
            j = self.skipSpace(s, i)
            if j < 0:
                return

            i = self.directiveOrStatement(s, j)
            if i < 0:
                # print("# next char: %s" % s)
                self.BadSyntax(s, j, "expected directive or statement")

    def directiveOrStatement(self, argstr: str, h: int) -> int:

        i = self.skipSpace(argstr, h)
        if i < 0:
            return i  # EOF

        if self.turtle:
            j = self.sparqlDirective(argstr, i)
            if j >= 0:
                return j

        j = self.directive(argstr, i)
        if j >= 0:
            return self.checkDot(argstr, j)

        j = self.statement(argstr, i)
        if j >= 0:
            return self.checkDot(argstr, j)

        return j

    # @@I18N
    # _namechars = string.lowercase + string.uppercase + string.digits + '_-'

    def tok(self, tok: str, argstr: str, i: int, colon: bool = False):
        """Check for keyword.  Space must have been stripped on entry and
        we must not be at end of file.
        if colon, then keyword followed by colon is ok
        (@prefix:<blah> is ok, rdf:type shortcut a must be followed by ws)
        """

        assert tok[0] not in _notNameChars  # not for punctuation
        if argstr[i] == "@":
            i += 1
        else:
            if tok not in self.keywords:
                return -1  # No, this has neither keywords declaration nor "@"

        i_plus_len_tok = i + len(tok)
        if (
            argstr[i:i_plus_len_tok] == tok
            and (argstr[i_plus_len_tok] in _notKeywordsChars)
            or (colon and argstr[i_plus_len_tok] == ":")
        ):
            return i_plus_len_tok
        else:
            return -1

    def sparqlTok(self, tok: str, argstr: str, i: int) -> int:
        """Check for SPARQL keyword.  Space must have been stripped on entry
        and we must not be at end of file.
        Case insensitive and not preceded by @
        """

        assert tok[0] not in _notNameChars  # not for punctuation

        len_tok = len(tok)
        if argstr[i : i + len_tok].lower() == tok.lower() and (
            argstr[i + len_tok] in _notQNameChars
        ):
            i += len_tok
            return i
        else:
            return -1

    def directive(self, argstr: str, i: int) -> int:
        j = self.skipSpace(argstr, i)
        if j < 0:
            return j  # eof
        res: typing.List[Any] = []

        j = self.tok("bind", argstr, i)  # implied "#". Obsolete.
        if j > 0:
            self.BadSyntax(argstr, i, "keyword bind is obsolete: use @prefix")

        j = self.tok("keywords", argstr, i)
        if j > 0:
            if self.turtle:
                self.BadSyntax(argstr, i, "Found 'keywords' when in Turtle mode.")

            i = self.commaSeparatedList(argstr, j, res, self.bareWord)
            if i < 0:
                self.BadSyntax(
                    argstr, i, "'@keywords' needs comma separated list of words"
                )
            self.setKeywords(res[:])
            return i

        j = self.tok("forAll", argstr, i)
        if j > 0:
            if self.turtle:
                self.BadSyntax(argstr, i, "Found 'forAll' when in Turtle mode.")

            i = self.commaSeparatedList(argstr, j, res, self.uri_ref2)
            if i < 0:
                self.BadSyntax(argstr, i, "Bad variable list after @forAll")
            for x in res:
                # self._context.declareUniversal(x)
                if x not in self._variables or x in self._parentVariables:
                    self._variables[x] = self._context.newUniversal(x)
            return i

        j = self.tok("forSome", argstr, i)
        if j > 0:
            if self.turtle:
                self.BadSyntax(argstr, i, "Found 'forSome' when in Turtle mode.")

            i = self.commaSeparatedList(argstr, j, res, self.uri_ref2)
            if i < 0:
                self.BadSyntax(argstr, i, "Bad variable list after @forSome")
            for x in res:
                self._context.declareExistential(x)
            return i

        j = self.tok("prefix", argstr, i, colon=True)  # no implied "#"
        if j >= 0:
            t: typing.List[Any] = []
            i = self.qname(argstr, j, t)
            if i < 0:
                self.BadSyntax(argstr, j, "expected qname after @prefix")
            j = self.uri_ref2(argstr, i, t)
            if j < 0:
                self.BadSyntax(argstr, i, "expected <uriref> after @prefix _qname_")
            ns = self.uriOf(t[1])

            if self._baseURI:
                ns = join(self._baseURI, ns)
            elif ":" not in ns:
                self.BadSyntax(
                    argstr,
                    j,
                    f"With no base URI, cannot use relative URI in @prefix <{ns}>",
                )
            assert ":" in ns  # must be absolute
            self._bindings[t[0][0]] = ns
            self.bind(t[0][0], hexify(ns))
            return j

        j = self.tok("base", argstr, i)  # Added 2007/7/7
        if j >= 0:
            t = []
            i = self.uri_ref2(argstr, j, t)
            if i < 0:
                self.BadSyntax(argstr, j, "expected <uri> after @base ")
            ns = self.uriOf(t[0])

            if self._baseURI:
                ns = join(self._baseURI, ns)
            else:
                self.BadSyntax(
                    argstr,
                    j,
                    "With no previous base URI, cannot use "
                    + "relative URI in @base  <"
                    + ns
                    + ">",
                )
            assert ":" in ns  # must be absolute
            self._baseURI = ns
            return i

        return -1  # Not a directive, could be something else.

    def sparqlDirective(self, argstr: str, i: int):

        """
        turtle and trig support BASE/PREFIX without @ and without
        terminating .
        """

        j = self.skipSpace(argstr, i)
        if j < 0:
            return j  # eof

        j = self.sparqlTok("PREFIX", argstr, i)
        if j >= 0:
            t: typing.List[Any] = []
            i = self.qname(argstr, j, t)
            if i < 0:
                self.BadSyntax(argstr, j, "expected qname after @prefix")
            j = self.uri_ref2(argstr, i, t)
            if j < 0:
                self.BadSyntax(argstr, i, "expected <uriref> after @prefix _qname_")
            ns = self.uriOf(t[1])

            if self._baseURI:
                ns = join(self._baseURI, ns)
            elif ":" not in ns:
                self.BadSyntax(
                    argstr,
                    j,
                    "With no base URI, cannot use "
                    + "relative URI in @prefix <"
                    + ns
                    + ">",
                )
            assert ":" in ns  # must be absolute
            self._bindings[t[0][0]] = ns
            self.bind(t[0][0], hexify(ns))
            return j

        j = self.sparqlTok("BASE", argstr, i)
        if j >= 0:
            t = []
            i = self.uri_ref2(argstr, j, t)
            if i < 0:
                self.BadSyntax(argstr, j, "expected <uri> after @base ")
            ns = self.uriOf(t[0])

            if self._baseURI:
                ns = join(self._baseURI, ns)
            else:
                self.BadSyntax(
                    argstr,
                    j,
                    "With no previous base URI, cannot use "
                    + "relative URI in @base  <"
                    + ns
                    + ">",
                )
            assert ":" in ns  # must be absolute
            self._baseURI = ns
            return i

        return -1  # Not a directive, could be something else.

    def bind(self, qn: str, uri: bytes) -> None:
        assert isinstance(uri, bytes), "Any unicode must be %x-encoded already"
        if qn == "":
            self._store.setDefaultNamespace(uri)
        else:
            self._store.bind(qn, uri)

    def setKeywords(self, k: Optional[typing.List[str]]):
        """Takes a list of strings"""
        if k is None:
            self.keywordsSet = 0
        else:
            self.keywords = k
            self.keywordsSet = 1

    def startDoc(self) -> None:
        # was: self._store.startDoc()
        self._store.startDoc(self._formula)

    def endDoc(self) -> Optional["Formula"]:
        """Signal end of document and stop parsing. returns formula"""
        self._store.endDoc(self._formula)  # don't canonicalize yet
        return self._formula

    def makeStatement(self, quadruple):
        # $$$$$$$$$$$$$$$$$$$$$
        # print "# Parser output: ", `quadruple`
        self._store.makeStatement(quadruple, why=self._reason2)

    def statement(self, argstr: str, i: int) -> int:
        r: typing.List[Any] = []
        i = self.object(argstr, i, r)  # Allow literal for subject - extends RDF
        if i < 0:
            return i

        j = self.property_list(argstr, i, r[0])

        if j < 0:
            self.BadSyntax(argstr, i, "expected propertylist")
        return j

    def subject(self, argstr: str, i: int, res: typing.List[Any]) -> int:
        return self.item(argstr, i, res)

    def verb(self, argstr: str, i: int, res: typing.List[Any]) -> int:
        """has _prop_
        is _prop_ of
        a
        =
        _prop_
        >- prop ->
        <- prop -<
        _operator_"""

        j = self.skipSpace(argstr, i)
        if j < 0:
            return j  # eof

        r: typing.List[Any] = []

        j = self.tok("has", argstr, i)
        if j >= 0:
            if self.turtle:
                self.BadSyntax(argstr, i, "Found 'has' keyword in Turtle mode")

            i = self.prop(argstr, j, r)
            if i < 0:
                self.BadSyntax(argstr, j, "expected property after 'has'")
            res.append(("->", r[0]))
            return i

        j = self.tok("is", argstr, i)
        if j >= 0:
            if self.turtle:
                self.BadSyntax(argstr, i, "Found 'is' keyword in Turtle mode")

            i = self.prop(argstr, j, r)
            if i < 0:
                self.BadSyntax(argstr, j, "expected <property> after 'is'")
            j = self.skipSpace(argstr, i)
            if j < 0:
                self.BadSyntax(
                    argstr, i, "End of file found, expected property after 'is'"
                )
            i = j
            j = self.tok("of", argstr, i)
            if j < 0:
                self.BadSyntax(argstr, i, "expected 'of' after 'is' <prop>")
            res.append(("<-", r[0]))
            return j

        j = self.tok("a", argstr, i)
        if j >= 0:
            res.append(("->", RDF_type))
            return j

        if argstr[i : i + 2] == "<=":
            if self.turtle:
                self.BadSyntax(argstr, i, "Found '<=' in Turtle mode. ")

            res.append(("<-", self._store.newSymbol(Logic_NS + "implies")))
            return i + 2

        if argstr[i] == "=":
            if self.turtle:
                self.BadSyntax(argstr, i, "Found '=' in Turtle mode")
            if argstr[i + 1] == ">":
                res.append(("->", self._store.newSymbol(Logic_NS + "implies")))
                return i + 2
            res.append(("->", DAML_sameAs))
            return i + 1

        if argstr[i : i + 2] == ":=":
            if self.turtle:
                self.BadSyntax(argstr, i, "Found ':=' in Turtle mode")

            # patch file relates two formulae, uses this    @@ really?
            res.append(("->", Logic_NS + "becomes"))
            return i + 2

        j = self.prop(argstr, i, r)
        if j >= 0:
            res.append(("->", r[0]))
            return j

        if argstr[i : i + 2] == ">-" or argstr[i : i + 2] == "<-":
            self.BadSyntax(argstr, j, ">- ... -> syntax is obsolete.")

        return -1

    def prop(self, argstr: str, i: int, res):
        return self.item(argstr, i, res)

    def item(self, argstr: str, i, res):
        return self.path(argstr, i, res)

    def blankNode(self, uri=None):
        return self._store.newBlankNode(self._context, uri, why=self._reason2)

    def path(self, argstr: str, i: int, res):
        """Parse the path production."""
        j = self.nodeOrLiteral(argstr, i, res)
        if j < 0:
            return j  # nope

        while argstr[j] in {"!", "^"}:  # no spaces, must follow exactly (?)
            ch = argstr[j]
            subj = res.pop()
            obj = self.blankNode(uri=self.here(j))
            j = self.node(argstr, j + 1, res)
            if j < 0:
                self.BadSyntax(argstr, j, "EOF found in middle of path syntax")
            pred = res.pop()
            if ch == "^":  # Reverse traverse
                self.makeStatement((self._context, pred, obj, subj))
            else:
                self.makeStatement((self._context, pred, subj, obj))
            res.append(obj)
        return j

    def anonymousNode(self, ln: str):
        """Remember or generate a term for one of these _: anonymous nodes"""
        if ("RdfstarTriple" in ln):

            term = self._rdfstartripleNodes.get(ln, None)
            if term is not None:
                return term
            term = self._store.newRdfstarTriple(self._context, why=self._reason2, hashvalue = ln)
            self._rdfstartripleNodes[ln] = term
            return term
        term = self._anonymousNodes.get(ln, None)
        if term is not None:
            return term
        term = self._store.newBlankNode(self._context, why=self._reason2)
        self._anonymousNodes[ln] = term
        return term

    def node(self, argstr: str, i: int, res, subjectAlready=None):
        """Parse the <node> production.
        Space is now skipped once at the beginning
        instead of in multiple calls to self.skipSpace().
        """
        subj = subjectAlready

        j = self.skipSpace(argstr, i)
        if j < 0:
            return j  # eof
        i = j
        ch = argstr[i]  # Quick 1-character checks first:

        if ch == "[":
            bnodeID = self.here(i)
            j = self.skipSpace(argstr, i + 1)
            if j < 0:
                self.BadSyntax(argstr, i, "EOF after '['")
            # Hack for "is" binding name to anon node
            if argstr[j] == "=":
                if self.turtle:
                    self.BadSyntax(
                        argstr, j, "Found '[=' or '[ =' when in turtle mode."
                    )
                i = j + 1
                objs: typing.List[Any] = []
                j = self.objectList(argstr, i, objs)
                if j >= 0:
                    subj = objs[0]
                    if len(objs) > 1:
                        for obj in objs:
                            self.makeStatement((self._context, DAML_sameAs, subj, obj))
                    j = self.skipSpace(argstr, j)
                    if j < 0:
                        self.BadSyntax(
                            argstr, i, "EOF when objectList expected after [ = "
                        )
                    if argstr[j] == ";":
                        j += 1
                else:
                    self.BadSyntax(argstr, i, "objectList expected after [= ")

            if subj is None:
                subj = self.blankNode(uri=bnodeID)

            i = self.property_list(argstr, j, subj)
            if i < 0:
                self.BadSyntax(argstr, j, "property_list expected")

            j = self.skipSpace(argstr, i)
            if j < 0:
                self.BadSyntax(
                    argstr, i, "EOF when ']' expected after [ <propertyList>"
                )
            if argstr[j] != "]":

                self.BadSyntax(argstr, j, "']' expected")
            res.append(subj)
            return j + 1

        if not self.turtle and ch == "{":
            # if self.turtle:
            #     self.BadSyntax(argstr, i,
            #                     "found '{' while in Turtle mode, Formulas not supported!")
            ch2 = argstr[i + 1]
            if ch2 == "$":
                # a set
                i += 1
                j = i + 1
                List = []
                first_run = True
                while 1:
                    i = self.skipSpace(argstr, j)
                    if i < 0:
                        self.BadSyntax(argstr, i, "needed '$}', found end.")
                    if argstr[i : i + 2] == "$}":
                        j = i + 2
                        break

                    if not first_run:
                        if argstr[i] == ",":
                            i += 1
                        else:
                            self.BadSyntax(argstr, i, "expected: ','")
                    else:
                        first_run = False

                    item: typing.List[Any] = []
                    j = self.item(argstr, i, item)  # @@@@@ should be path, was object
                    if j < 0:
                        self.BadSyntax(argstr, i, "expected item in set or '$}'")
                    List.append(self._store.intern(item[0]))
                res.append(self._store.newSet(List, self._context))
                return j
            else:
                # parse a formula
                j = i + 1
                oldParentContext = self._parentContext
                self._parentContext = self._context
                parentAnonymousNodes = self._anonymousNodes
                grandParentVariables = self._parentVariables
                self._parentVariables = self._variables
                self._anonymousNodes = {}
                self._variables = self._variables.copy()
                reason2 = self._reason2
                self._reason2 = becauseSubexpression
                if subj is None:
                    subj = self._store.newFormula()
                self._context = subj

                while 1:
                    i = self.skipSpace(argstr, j)
                    if i < 0:
                        self.BadSyntax(argstr, i, "needed '}', found end.")

                    if argstr[i] == "}":
                        j = i + 1
                        break

                    j = self.directiveOrStatement(argstr, i)
                    if j < 0:
                        self.BadSyntax(argstr, i, "expected statement or '}'")

                self._anonymousNodes = parentAnonymousNodes
                self._variables = self._parentVariables
                self._parentVariables = grandParentVariables
                self._context = self._parentContext
                self._reason2 = reason2
                self._parentContext = oldParentContext
                res.append(subj.close())  # No use until closed
                return j

        if ch == "(":
            thing_type = self._store.newList
            ch2 = argstr[i + 1]
            if ch2 == "$":
                thing_type = self._store.newSet
                i += 1
            j = i + 1

            List = []
            while 1:
                i = self.skipSpace(argstr, j)
                if i < 0:
                    self.BadSyntax(argstr, i, "needed ')', found end.")
                if argstr[i] == ")":
                    j = i + 1
                    break

                item = []
                j = self.item(argstr, i, item)  # @@@@@ should be path, was object
                if j < 0:
                    self.BadSyntax(argstr, i, "expected item in list or ')'")
                List.append(self._store.intern(item[0]))
            res.append(thing_type(List, self._context))
            return j

        j = self.tok("this", argstr, i)  # This context
        if j >= 0:
            self.BadSyntax(
                argstr,
                i,
                "Keyword 'this' was ancient N3. Now use "
                + "@forSome and @forAll keywords.",
            )

        # booleans
        j = self.tok("true", argstr, i)
        if j >= 0:
            res.append(True)
            return j
        j = self.tok("false", argstr, i)
        if j >= 0:
            res.append(False)
            return j

        if subj is None:  # If this can be a named node, then check for a name.
            j = self.uri_ref2(argstr, i, res)
            if j >= 0:
                return j

        return -1

    def addingquotedRdfstarTriple(self, quoted_triple_list, dira):
        if quoted_triple_list[0] == rdflib.term.URIRef('https://w3id.org/rdf-star/AssertedStatement'):
            if quoted_triple_list[1] == rdflib.term.URIRef('https://w3id.org/rdf-star/QuotedStatement'):
                if dira == "->":
                    self.makeStatement((self._context, quoted_triple_list[4], quoted_triple_list[3], quoted_triple_list[5]))
                    quoted_triple_list[2].setSubject(quoted_triple_list[3])
                    quoted_triple_list[2].setPredicate(quoted_triple_list[4])
                    quoted_triple_list[2].setObject(quoted_triple_list[5])

                else:
                    self.makeStatement((self._context, quoted_triple_list[4], quoted_triple_list[5], quoted_triple_list[3]))

                    quoted_triple_list[2].setSubject(quoted_triple_list[4])
                    quoted_triple_list[2].setPredicate(quoted_triple_list[5])
                    quoted_triple_list[2].setObject(quoted_triple_list[6])

            else:
                if dira == "->":
                    self.makeStatement((self._context, quoted_triple_list[2], quoted_triple_list[1], quoted_triple_list[3]))
                else:
                    self.makeStatement((self._context, quoted_triple_list[2], quoted_triple_list[3], quoted_triple_list[1]))
        else:
            if dira == "->":
                quoted_triple_list[1].setSubject(quoted_triple_list[2])
                quoted_triple_list[1].setPredicate(quoted_triple_list[3])
                quoted_triple_list[1].setObject(quoted_triple_list[4])

            else:
                quoted_triple_list[1].setSubject(quoted_triple_list[2])
                quoted_triple_list[1].setPredicate(quoted_triple_list[3])
                quoted_triple_list[1].setObject(quoted_triple_list[4])

    def property_list(self, argstr: str, i: int, subj):
        """Parse property list
        Leaves the terminating punctuation in the buffer
        """
        global quoted_triple_list
        while 1:
            while 1:  # skip repeat ;
                j = self.skipSpace(argstr, i)
                if j < 0:
                    self.BadSyntax(
                        argstr, i, "EOF found when expected verb in property list"
                    )
                if argstr[j] != ";":
                    break
                i = j + 1

            if argstr[j : j + 2] == ":-":
                if self.turtle:
                    self.BadSyntax(argstr, j, "Found in ':-' in Turtle mode")
                i = j + 2
                res: typing.List[Any] = []
                j = self.node(argstr, i, res, subj)
                if j < 0:
                    self.BadSyntax(argstr, i, "bad {} or () or [] node after :- ")
                i = j
                continue
            i = j
            v: typing.List[Any] = []
            j = self.verb(argstr, i, v)
            if j <= 0:
                return i  # void but valid

            objs: typing.List[Any] = []
            i = self.objectList(argstr, j, objs)
            if i < 0:
                self.BadSyntax(argstr, j, "objectList expected")

            for obj in objs:
                dira, sym = v[0]
                if "RdfstarTriple" in subj:
                    if "rdf-star" in str(obj):
                        if len(quoted_triple_list) > 2:
                            quoted_triple_list = []
                    quoted_triple_list.append(obj)
                    if (rdflib.term.URIRef('https://w3id.org/rdf-star/QuotedStatement') in quoted_triple_list) & (not (subj in quoted_triple_list)):
                        quoted_triple_list.append(subj)
                    if "#object" in sym:
                        self.addingquotedRdfstarTriple(quoted_triple_list, dira)
                else:
                    if dira == "->":
                        self.makeStatement((self._context, sym, subj, obj))
                    else:
                        self.makeStatement((self._context, sym, obj, subj))

            j = self.skipSpace(argstr, i)
            if j < 0:
                self.BadSyntax(argstr, j, "EOF found in list of objects")
            if argstr[i] != ";":
                return i
            i += 1  # skip semicolon and continue

    def commaSeparatedList(self, argstr: str, j, res, what):
        """return value: -1 bad syntax; >1 new position in argstr
        res has things found appended
        """
        i = self.skipSpace(argstr, j)
        if i < 0:
            self.BadSyntax(argstr, i, "EOF found expecting comma sep list")
        if argstr[i] == ".":
            return j  # empty list is OK
        i = what(argstr, i, res)
        if i < 0:
            return -1

        while 1:
            j = self.skipSpace(argstr, i)
            if j < 0:
                return j  # eof
            ch = argstr[j]
            if ch != ",":
                if ch != ".":
                    return -1
                return j  # Found  but not swallowed "."
            i = what(argstr, j + 1, res)
            if i < 0:
                self.BadSyntax(argstr, i, "bad list content")

    def objectList(self, argstr: str, i: int, res: typing.List[Any]) -> int:
        i = self.object(argstr, i, res)
        if i < 0:
            return -1
        while 1:
            j = self.skipSpace(argstr, i)
            if j < 0:
                self.BadSyntax(argstr, j, "EOF found after object")
            if argstr[j] != ",":
                return j  # Found something else!
            i = self.object(argstr, j + 1, res)
            if i < 0:
                return i

    def checkDot(self, argstr: str, i: int):
        j = self.skipSpace(argstr, i)
        if j < 0:
            return j  # eof
        ch = argstr[j]
        if ch == ".":
            return j + 1  # skip
        if ch == "}":
            return j  # don't skip it
        if ch == "]":
            return j
        self.BadSyntax(argstr, j, "expected '.' or '}' or ']' at end of statement")

    def uri_ref2(self, argstr: str, i: int, res):
        """Generate uri from n3 representation.
        Note that the RDF convention of directly concatenating
        NS and local name is now used though I prefer inserting a '#'
        to make the namesapces look more like what XML folks expect.
        """
        qn: typing.List[Any] = []
        j = self.qname(argstr, i, qn)
        if j >= 0:
            pfx, ln = qn[0]
            if pfx is None:
                assert 0, "not used?"
                ns = self._baseURI + ADDED_HASH  # type: ignore[unreachable]
            else:
                try:
                    ns = self._bindings[pfx]
                except KeyError:
                    if pfx == "_":  # Magic prefix 2001/05/30, can be changed
                        res.append(self.anonymousNode(ln))
                        return j
                    if not self.turtle and pfx == "":
                        ns = join(self._baseURI or "", "#")
                    else:
                        self.BadSyntax(argstr, i, 'Prefix "%s:" not bound' % (pfx))
            symb = self._store.newSymbol(ns + ln)
            res.append(self._variables.get(symb, symb))
            return j

        i = self.skipSpace(argstr, i)
        if i < 0:
            return -1

        if argstr[i] == "?":
            v: typing.List[Any] = []
            j = self.variable(argstr, i, v)
            if j > 0:  # Forget variables as a class, only in context.
                res.append(v[0])
                return j
            return -1

        elif argstr[i] == "<":
            st = i + 1
            i = argstr.find(">", st)
            if i >= 0:
                uref = argstr[st:i]  # the join should dealt with "":

                # expand unicode escapes
                uref = unicodeEscape8.sub(unicodeExpand, uref)
                uref = unicodeEscape4.sub(unicodeExpand, uref)

                if self._baseURI:
                    uref = join(self._baseURI, uref)  # was: uripath.join
                else:
                    assert (
                        ":" in uref
                    ), "With no base URI, cannot deal with relative URIs"
                if argstr[i - 1] == "#" and not uref[-1:] == "#":
                    uref += "#"  # She meant it! Weirdness in urlparse?
                symb = self._store.newSymbol(uref)
                res.append(self._variables.get(symb, symb))
                return i + 1
            self.BadSyntax(argstr, j, "unterminated URI reference")

        elif self.keywordsSet:
            v = []
            j = self.bareWord(argstr, i, v)
            if j < 0:
                return -1  # Forget variables as a class, only in context.
            if v[0] in self.keywords:
                self.BadSyntax(argstr, i, 'Keyword "%s" not allowed here.' % v[0])
            res.append(self._store.newSymbol(self._bindings[""] + v[0]))
            return j
        else:
            return -1

    def skipSpace(self, argstr: str, i: int):
        """Skip white space, newlines and comments.
        return -1 if EOF, else position of first non-ws character"""

        # Most common case is a non-commented line starting with few spaces and tabs.
        try:
            while True:
                ch = argstr[i]
                if ch in {" ", "\t"}:
                    i += 1
                    continue
                elif ch not in {"#", "\r", "\n"}:
                    return i
                break
        except IndexError:
            return -1

        while 1:
            m = eol.match(argstr, i)
            if m is None:
                break
            self.lines += 1
            self.startOfLine = i = m.end()  # Point to first character unmatched
        m = ws.match(argstr, i)
        if m is not None:
            i = m.end()
        m = eof.match(argstr, i)
        return i if m is None else -1

    def variable(self, argstr: str, i: int, res):
        """?abc -> variable(:abc)"""

        j = self.skipSpace(argstr, i)
        if j < 0:
            return -1

        if argstr[j] != "?":
            return -1
        j += 1
        i = j
        if argstr[j] in numberChars:
            self.BadSyntax(argstr, j, "Variable name can't start with '%s'" % argstr[j])
        len_argstr = len(argstr)
        while i < len_argstr and argstr[i] not in _notKeywordsChars:
            i += 1
        if self._parentContext is None:
            varURI = self._store.newSymbol(self._baseURI + "#" + argstr[j:i])  # type: ignore[operator]
            if varURI not in self._variables:
                self._variables[varURI] = self._context.newUniversal(
                    varURI, why=self._reason2
                )
            res.append(self._variables[varURI])
            return i
            # @@ was:
            # self.BadSyntax(argstr, j,
            #     "Can't use ?xxx syntax for variable in outermost level: %s"
            #     % argstr[j-1:i])
        varURI = self._store.newSymbol(self._baseURI + "#" + argstr[j:i])  # type: ignore[operator]
        if varURI not in self._parentVariables:
            self._parentVariables[varURI] = self._parentContext.newUniversal(
                varURI, why=self._reason2
            )
        res.append(self._parentVariables[varURI])
        return i

    def bareWord(self, argstr: str, i: int, res):
        """abc -> :abc"""
        j = self.skipSpace(argstr, i)
        if j < 0:
            return -1

        if argstr[j] in numberChars or argstr[j] in _notKeywordsChars:
            return -1
        i = j
        len_argstr = len(argstr)
        while i < len_argstr and argstr[i] not in _notKeywordsChars:
            i += 1
        res.append(argstr[j:i])
        return i

    def qname(self, argstr: str, i: int, res):
        """
        xyz:def -> ('xyz', 'def')
        If not in keywords and keywordsSet: def -> ('', 'def')
        :def -> ('', 'def')
        """

        i = self.skipSpace(argstr, i)
        if i < 0:
            return -1

        c = argstr[i]
        if c in numberCharsPlus:
            return -1
        len_argstr = len(argstr)
        if c not in _notNameChars:
            j = i
            i += 1

            try:
                while argstr[i] not in _notNameChars:
                    i += 1
            except IndexError:
                pass  # Very rare.

            if argstr[i - 1] == ".":  # qname cannot end with "."
                i -= 1
                if i == j:
                    return -1
            ln = argstr[j:i]

        else:  # First character is non-alpha
            ln = ""  # Was:  None - TBL (why? useful?)

        if i < len_argstr and argstr[i] == ":":
            pfx = ln
            # bnodes names have different rules
            if pfx == "_":
                allowedChars = _notNameChars
            else:
                allowedChars = _notQNameChars

            i += 1
            lastslash = False
            start = i
            ln = ""
            while i < len_argstr:
                c = argstr[i]
                if c == "\\" and not lastslash:  # Very rare.
                    lastslash = True
                    if start < i:
                        ln += argstr[start:i]
                    start = i + 1
                elif c not in allowedChars or lastslash:  # Most common case is "a-zA-Z"
                    if lastslash:
                        if c not in escapeChars:
                            raise BadSyntax(
                                self._thisDoc,
                                self.lines,
                                argstr,
                                i,
                                "illegal escape " + c,
                            )
                    elif c == "%":  # Very rare.
                        if (
                            argstr[i + 1] not in hexChars
                            or argstr[i + 2] not in hexChars
                        ):
                            raise BadSyntax(
                                self._thisDoc,
                                self.lines,
                                argstr,
                                i,
                                "illegal hex escape " + c,
                            )
                    lastslash = False
                else:
                    break
                i += 1

            if lastslash:
                raise BadSyntax(
                    self._thisDoc, self.lines, argstr, i, "qname cannot end with \\"
                )

            if argstr[i - 1] == ".":
                # localname cannot end in .
                if len(ln) == 0 and start == i:
                    return -1
                i -= 1

            if start < i:
                ln += argstr[start:i]

            res.append((pfx, ln))
            return i

        else:  # delimiter was not ":"
            if ln and self.keywordsSet and ln not in self.keywords:
                res.append(("", ln))
                return i
            return -1

    def object(self, argstr: str, i: int, res):
        j = self.subject(argstr, i, res)
        if j >= 0:
            return j
        else:
            j = self.skipSpace(argstr, i)
            if j < 0:
                return -1
            else:
                i = j

            ch = argstr[i]
            if ch in self.string_delimiters:
                ch_three = ch * 3
                if argstr[i : i + 3] == ch_three:
                    delim = ch_three
                    i += 3
                else:
                    delim = ch
                    i += 1

                j, s = self.strconst(argstr, i, delim)

                res.append(self._store.newLiteral(s))  # type: ignore[call-arg] # TODO FIXME
                return j
            else:
                return -1

    def nodeOrLiteral(self, argstr: str, i: int, res):
        j = self.node(argstr, i, res)
        startline = self.lines  # Remember where for error messages
        if j >= 0:
            return j
        else:
            j = self.skipSpace(argstr, i)
            if j < 0:
                return -1
            else:
                i = j

            ch = argstr[i]
            if ch in numberCharsPlus:
                m = exponent_syntax.match(argstr, i)
                if m:
                    j = m.end()
                    res.append(float(argstr[i:j]))
                    return j

                m = decimal_syntax.match(argstr, i)
                if m:
                    j = m.end()
                    res.append(Decimal(argstr[i:j]))
                    return j

                m = integer_syntax.match(argstr, i)
                if m:
                    j = m.end()
                    res.append(long_type(argstr[i:j]))
                    return j

                # return -1  ## or fall through?

            ch_three = ch * 3
            if ch in self.string_delimiters:
                if argstr[i : i + 3] == ch_three:
                    delim = ch_three
                    i += 3
                else:
                    delim = ch
                    i += 1

                dt = None
                j, s = self.strconst(argstr, i, delim)
                lang = None
                if argstr[j] == "@":  # Language?
                    m = langcode.match(argstr, j + 1)
                    if m is None:
                        raise BadSyntax(
                            self._thisDoc,
                            startline,
                            argstr,
                            i,
                            "Bad language code syntax on string " + "literal, after @",
                        )
                    i = m.end()
                    lang = argstr[j + 1 : i]
                    j = i
                if argstr[j : j + 2] == "^^":
                    res2: typing.List[Any] = []
                    j = self.uri_ref2(argstr, j + 2, res2)  # Read datatype URI
                    dt = res2[0]
                res.append(self._store.newLiteral(s, dt, lang))
                return j
            else:
                return -1

    def uriOf(self, sym):
        if isinstance(sym, tuple):
            return sym[1]  # old system for --pipe
        # return sym.uriref()  # cwm api
        return sym

    def strconst(self, argstr: str, i: int, delim):
        """parse an N3 string constant delimited by delim.
        return index, val
        """
        delim1 = delim[0]
        delim2, delim3, delim4, delim5 = delim1 * 2, delim1 * 3, delim1 * 4, delim1 * 5

        j = i
        ustr = ""  # Empty unicode string
        startline = self.lines  # Remember where for error messages
        len_argstr = len(argstr)
        while j < len_argstr:
            if argstr[j] == delim1:
                if delim == delim1:  # done when delim is " or '
                    i = j + 1
                    return i, ustr
                if (
                    delim == delim3
                ):  # done when delim is """ or ''' and, respectively ...
                    if argstr[j : j + 5] == delim5:  # ... we have "" or '' before
                        i = j + 5
                        ustr += delim2
                        return i, ustr
                    if argstr[j : j + 4] == delim4:  # ... we have " or ' before
                        i = j + 4
                        ustr += delim1
                        return i, ustr
                    if argstr[j : j + 3] == delim3:  # current " or ' is part of delim
                        i = j + 3
                        return i, ustr

                    # we are inside of the string and current char is " or '
                    j += 1
                    ustr += delim1
                    continue

            m = interesting.search(argstr, j)  # was argstr[j:].
            # Note for pos param to work, MUST be compiled  ... re bug?
            assert m, "Quote expected in string at ^ in %s^%s" % (
                argstr[j - 20 : j],
                argstr[j : j + 20],
            )  # at least need a quote

            i = m.start()
            try:
                ustr += argstr[j:i]
            except UnicodeError:
                err = ""
                for c in argstr[j:i]:
                    err = err + (" %02x" % ord(c))
                streason = sys.exc_info()[1].__str__()
                raise BadSyntax(
                    self._thisDoc,
                    startline,
                    argstr,
                    j,
                    "Unicode error appending characters"
                    + " %s to string, because\n\t%s" % (err, streason),
                )

            # print "@@@ i = ",i, " j=",j, "m.end=", m.end()

            ch = argstr[i]
            if ch == delim1:
                j = i
                continue
            elif ch in {'"', "'"} and ch != delim1:
                ustr += ch
                j = i + 1
                continue
            elif ch in {"\r", "\n"}:
                if delim == delim1:
                    raise BadSyntax(
                        self._thisDoc,
                        startline,
                        argstr,
                        i,
                        "newline found in string literal",
                    )
                self.lines += 1
                ustr += ch
                j = i + 1
                self.startOfLine = j

            elif ch == "\\":
                j = i + 1
                ch = argstr[j]  # Will be empty if string ends
                if not ch:
                    raise BadSyntax(
                        self._thisDoc,
                        startline,
                        argstr,
                        i,
                        "unterminated string literal (2)",
                    )
                k = "abfrtvn\\\"'".find(ch)
                if k >= 0:
                    uch = "\a\b\f\r\t\v\n\\\"'"[k]
                    ustr += uch
                    j += 1
                elif ch == "u":
                    j, ch = self.uEscape(argstr, j + 1, startline)
                    ustr += ch
                elif ch == "U":
                    j, ch = self.UEscape(argstr, j + 1, startline)
                    ustr += ch
                else:
                    self.BadSyntax(argstr, i, "bad escape")

        self.BadSyntax(argstr, i, "unterminated string literal")

    def _unicodeEscape(self, argstr: str, i, startline, reg, n, prefix):
        if len(argstr) < i + n:
            raise BadSyntax(
                self._thisDoc, startline, argstr, i, "unterminated string literal(3)"
            )
        try:
            return i + n, reg.sub(unicodeExpand, "\\" + prefix + argstr[i : i + n])
        except:
            raise BadSyntax(
                self._thisDoc,
                startline,
                argstr,
                i,
                "bad string literal hex escape: " + argstr[i : i + n],
            )

    def uEscape(self, argstr: str, i, startline):
        return self._unicodeEscape(argstr, i, startline, unicodeEscape4, 4, "u")

    def UEscape(self, argstr: str, i, startline):
        return self._unicodeEscape(argstr, i, startline, unicodeEscape8, 8, "U")

    def BadSyntax(self, argstr: str, i, msg):
        raise BadSyntax(self._thisDoc, self.lines, argstr, i, msg)


# If we are going to do operators then they should generate
# [  is  operator:plus  of (  \1  \2 ) ]


class BadSyntax(SyntaxError):
    def __init__(self, uri, lines, argstr, i, why):
        self._str = argstr.encode("utf-8")  # Better go back to strings for errors
        self._i = i
        self._why = why
        self.lines = lines
        self._uri = uri

    def __str__(self):
        argstr = self._str
        i = self._i
        st = 0
        if i > 60:
            pre = "..."
            st = i - 60
        else:
            pre = ""
        if len(argstr) - i > 60:
            post = "..."
        else:
            post = ""

        return 'at line %i of <%s>:\nBad syntax (%s) at ^ in:\n"%s%s^%s%s"' % (
            self.lines + 1,
            self._uri,
            self._why,
            pre,
            argstr[st:i],
            argstr[i : i + 60],
            post,
        )

    @property
    def message(self):
        return str(self)


###############################################################################
class Formula(object):
    number = 0

    def __init__(self, parent):
        self.uuid = uuid4().hex
        self.counter = 0
        Formula.number += 1
        self.number = Formula.number
        self.existentials = {}
        self.universals = {}

        self.quotedgraph = QuotedGraph(store=parent.store, identifier=self.id())

    def __str__(self):
        return "_:Formula%s" % self.number

    def id(self):
        return BNode("_:Formula%s" % self.number)

    def newBlankNode(self, uri=None, why=None):
        if uri is None:
            self.counter += 1
            bn = BNode("f%sb%s" % (self.uuid, self.counter))
        else:
            bn = BNode(uri.split("#").pop().replace("_", "b"))
        return bn

    def newRdfstarTriple(self, hashvalue, uri=None, why=None):
        if uri is None:
            # self.counter += 1
            rdfstartriple = RdfstarTriple(hashvalue = hashvalue)
        else:
            rdfstartriple = RdfstarTriple(hashvalue = hashvalue)
        return rdfstartriple

    def newUniversal(self, uri, why=None):
        return Variable(uri.split("#").pop())

    def declareExistential(self, x):
        self.existentials[x] = self.newBlankNode()

    def close(self):

        return self.quotedgraph


r_hibyte = re.compile(r"([\x80-\xff])")


class RDFSink(object):
    def __init__(self, graph: Graph):
        self.rootFormula: Optional[Formula] = None
        self.uuid = uuid4().hex
        self.counter = 0
        self.graph = graph

    def newFormula(self) -> Formula:
        fa = getattr(self.graph.store, "formula_aware", False)
        if not fa:
            raise ParserError(
                "Cannot create formula parser with non-formula-aware store."
            )
        f = Formula(self.graph)
        return f

    def newGraph(self, identifier: Identifier) -> Graph:
        return Graph(self.graph.store, identifier)

    def newSymbol(self, *args: str):
        return URIRef(args[0])

    def newBlankNode(
        self,
        arg: Optional[Union[Formula, Graph, Any]] = None,
        uri: Optional[str] = None,
        why: Optional[Callable[[], None]] = None,
    ) -> BNode:
        if isinstance(arg, Formula):
            return arg.newBlankNode(uri)
        elif isinstance(arg, Graph) or arg is None:
            self.counter += 1
            bn = BNode("n%sb%s" % (self.uuid, self.counter))
        else:
            bn = BNode(str(arg[0]).split("#").pop().replace("_", "b"))
        return bn

    def newRdfstarTriple(
        self,
        # hashvalue: Optional[str],
        # arg: Optional[Union[Formula, Graph, Any]] = None,
        # uri: Optional[str] = None,
        arg: Optional[Union[Formula, Graph, Any]] = None,
        uri: Optional[str] = None,
        why: Optional[Callable[[], None]] = None,
        hashvalue: Optional[str] = None
    ) -> RdfstarTriple:

        if isinstance(arg, Formula):

            return arg.newRdfstarTriple(hashvalue = hashvalue)
        elif isinstance(arg, Graph) or arg is None:

            rdfstartriple = RdfstarTriple(hashvalue =hashvalue)
        else:


            rdfstartriple = RdfstarTriple(hashvalue =hashvalue)
        return rdfstartriple

    def newLiteral(self, s: str, dt: Optional[URIRef], lang: Optional[str]) -> Literal:
        if dt:
            return Literal(s, datatype=dt)
        else:
            return Literal(s, lang=lang)

    def newList(self, n: typing.List[Any], f: Optional[Formula]):
        nil = self.newSymbol("http://www.w3.org/1999/02/22-rdf-syntax-ns#nil")
        if not n:
            return nil

        first = self.newSymbol("http://www.w3.org/1999/02/22-rdf-syntax-ns#first")
        rest = self.newSymbol("http://www.w3.org/1999/02/22-rdf-syntax-ns#rest")
        af = a = self.newBlankNode(f)

        for ne in n[:-1]:
            self.makeStatement((f, first, a, ne))
            an = self.newBlankNode(f)
            self.makeStatement((f, rest, a, an))
            a = an
        self.makeStatement((f, first, a, n[-1]))
        self.makeStatement((f, rest, a, nil))
        return af

    def newSet(self, *args):
        return set(args)

    def setDefaultNamespace(self, *args) -> str:
        return ":".join(repr(n) for n in args)

    def makeStatement(self, quadruple, why=None) -> None:
        f, p, s, o = quadruple

        if hasattr(p, "formula"):
            raise ParserError("Formula used as predicate")

        s = self.normalise(f, s)
        p = self.normalise(f, p)
        o = self.normalise(f, o)

        if f == self.rootFormula:
            # print s, p, o, '.'
            self.graph.add((s, p, o))
        elif isinstance(f, Formula):
            f.quotedgraph.add((s, p, o))
        else:
            f.add((s, p, o))

        # return str(quadruple)

    def normalise(self, f: Optional[Formula], n):
        if isinstance(n, tuple):
            return URIRef(str(n[1]))

        if isinstance(n, bool):
            s = Literal(str(n).lower(), datatype=BOOLEAN_DATATYPE)
            return s

        if isinstance(n, int) or isinstance(n, long_type):
            s = Literal(str(n), datatype=INTEGER_DATATYPE)
            return s

        if isinstance(n, Decimal):
            value = str(n)
            if value == "-0":
                value = "0"
            s = Literal(value, datatype=DECIMAL_DATATYPE)
            return s

        if isinstance(n, float):
            s = Literal(str(n), datatype=DOUBLE_DATATYPE)
            return s

        if isinstance(f, Formula):
            if n in f.existentials:
                return f.existentials[n]

        # if isinstance(n, Var):
        #    if f.universals.has_key(n):
        #       return f.universals[n]
        #    f.universals[n] = f.newBlankNode()
        #    return f.universals[n]

        return n

    def intern(self, something: AnyT) -> AnyT:
        return something

    def bind(self, pfx, uri):
        pass  # print pfx, ':', uri

    def startDoc(self, formula: Optional[Formula]):
        self.rootFormula = formula

    def endDoc(self, formula: Optional[Formula]) -> None:
        pass


###################################################
#
#  Utilities
#


def hexify(ustr):
    """Use URL encoding to return an ASCII string
    corresponding to the given UTF8 string
    >>> hexify("http://example/a b")
    b'http://example/a%20b'
    """
    # s1=ustr.encode('utf-8')
    s = ""
    for ch in ustr:  # .encode('utf-8'):
        if ord(ch) > 126 or ord(ch) < 33:
            ch = "%%%02X" % ord(ch)
        else:
            ch = "%c" % ord(ch)
        s = s + ch
    return s.encode("latin-1")


class NtriplesStarParser(Parser):

    """
    An RDFLib parser for Turtle
    See http://www.w3.org/TR/turtle/
    """

    def __init__(self):
        pass

    def parse(
        self,
        source: "InputSource",
        graph: Graph,
        encoding: Optional[str] = "utf-8",
        turtle: bool = True,
    ):
        if encoding not in [None, "utf-8"]:
            raise ParserError(
                "N3/Turtle files are always utf-8 encoded, I was passed: %s" % encoding
            )

        sink = RDFSink(graph)

        baseURI = graph.absolutize(source.getPublicId() or source.getSystemId() or "")
        p = SinkParser(sink, baseURI=baseURI, turtle=turtle)
        # N3 parser prefers str stream
        # stream = source.getCharacterStream()
        # if not stream:
        #     stream = source.getByteStream()
        # p.loadStream(stream)

        if hasattr(source, "file"):
            f = open(source.file.name, "rb")
            rdbytes = f.read()
            f.close()
        elif hasattr(source, "_InputSource__bytefile"):
            if hasattr(source._InputSource__bytefile, "wrapped"):
                f = open((source._InputSource__bytefile.wrapped.strip().splitlines())[0], "rb") # what if multiple files
                rdbytes = f.read()
                f.close()

        bp = rdbytes.decode("utf-8")
        if "<<" or "{|" in bp:
            ou = RDFstarParsings(bp)
        else:
            ou = bp
        ou = RDFstarParsings(bp)
        p.feed(ou)
        p.endDoc()
        for prefix, namespace in p._bindings.items():
            graph.bind(prefix, namespace)


class N3Parser(NtriplesStarParser):

    """
    An RDFLib parser for Notation3
    See http://www.w3.org/DesignIssues/Notation3.html
    """

    def __init__(self):
        pass

    def parse(self, source, graph, encoding="utf-8"):
        # we're currently being handed a Graph, not a ConjunctiveGraph
        # context-aware is this implied by formula_aware
        ca = getattr(graph.store, "context_aware", False)
        fa = getattr(graph.store, "formula_aware", False)
        if not ca:
            raise ParserError("Cannot parse N3 into non-context-aware store.")
        elif not fa:
            raise ParserError("Cannot parse N3 into non-formula-aware store.")

        conj_graph = ConjunctiveGraph(store=graph.store)
        conj_graph.default_context = graph  # TODO: CG __init__ should have a
        # default_context arg
        # TODO: update N3Processor so that it can use conj_graph as the sink
        conj_graph.namespace_manager = graph.namespace_manager

        NtriplesStarParser.parse(self, source, conj_graph, encoding, turtle=False)
