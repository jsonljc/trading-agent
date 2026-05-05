from agent.traders.registry import TraderRegistry


def test_registry_loads_urkel_and_pup_danny():
    reg = TraderRegistry.from_dir("config/traders")
    handles = {p.handle for p in reg.all()}
    assert "urkel" in handles
    assert "pup-danny" in handles
    urkel = next(p for p in reg.all() if p.handle == "urkel")
    assert urkel.auto_execute is True
    assert urkel.conviction_examples == ()
