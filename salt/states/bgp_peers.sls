# Apply generated BGP config to FRR devices.
# Expects the rendered config (from `python generate.py`) to be available at
# salt://output/{{ grains['id'] }}.conf — copy your output/ dir to the salt
# fileserver root before running `salt '*' state.apply bgp_peers`.

bgp_config:
  file.managed:
    - name: /etc/frr/frr.conf
    - source: salt://output/{{ grains['id'] }}.conf
    - template: jinja
    - user: root
    - group: root
    - mode: 644

frr_service:
  service.running:
    - name: frr
    - enable: True
    - watch:
      - file: bgp_config
