bgp_config:
  file.managed:
    - name: /etc/frr/frr.conf
    - source: salt://bgp_peers/frr.conf
    - user: root
    - group: root
    - mode: 644

frr_service:
  service.running:
    - name: frr
    - enable: True
    - watch:
      - file: bgp_config
