# -*- coding: utf-8 -*-
from firststage.protocol.acl_messages import AclMessage, make_acl, normalize_performative, VALID_PERFORMATIVES

def test_normalize_variants():
    assert normalize_performative("request") == "REQUEST"
    assert normalize_performative("Request_When") == "REQUEST-WHEN"
    assert normalize_performative("acceptproposal") == "ACCEPT-PROPOSAL"

def test_make_acl_serializes_and_defaults():
    s = make_acl("query-ref","A","B", content={"x":1}, conversation_id="c1", reply_with="r1")
    m = AclMessage.loads(s)
    assert m.performative == "QUERY-REF"
    assert m.protocol == "fipa-query"
    assert m.conversation_id == "c1"
    assert m.reply_with == "r1"
    assert m.content["x"] == 1

def test_all_performatives_accepted():
    for pf in VALID_PERFORMATIVES:
        s = make_acl(pf,"A","B",content={"ok":True})
        m = AclMessage.loads(s)
        assert m.performative == pf
