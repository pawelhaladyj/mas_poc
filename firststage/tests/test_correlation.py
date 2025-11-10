# -*- coding: utf-8 -*-
from firststage.protocol.correlation import CorrBook

def test_corr_pass_initiating_frames():
    corr = CorrBook()
    assert corr.match_and_pop("c1", None, from_bare="x@d", performative="INFORM")

def test_corr_register_and_match():
    corr = CorrBook()
    corr.register("c1","r1", allow_from=["s@d"], allow_pf=["INFORM"], ttl_sec=2)
    assert not corr.match_and_pop("c1","r1", from_bare="z@d", performative="INFORM")
    assert not corr.match_and_pop("c1","r1", from_bare="s@d", performative="AGREE")
    assert corr.match_and_pop("c1","r1", from_bare="s@d", performative="INFORM")
    # drugi raz już nie przejdzie (pop)
    assert not corr.match_and_pop("c1","r1", from_bare="s@d", performative="INFORM")
