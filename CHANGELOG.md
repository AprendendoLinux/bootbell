# Histórico de versões

O formato segue o [Keep a Changelog](https://keepachangelog.com/pt-BR/1.1.0/)
e o projeto usa [versionamento semântico](https://semver.org/lang/pt-BR/).

## [1.0.1] - 2026-11-22

### Corrigido
- O aviso de "ligado" podia sair sem o endereço IPv4 quando a rede era dada
  como pronta antes do DHCP terminar (ex.: IPv6 fixo e IPv4 por DHCP no
  NetworkManager). Agora o bootbell espera até `ip_wait` segundos (padrão 60)
  por um IPv4 antes de montar o aviso. O boot não é atrasado.

### Adicionado
- Opção `ip_wait` em `[general]`.

## [1.0.0] - 2026-11-19

Primeira versão.

### Adicionado
- Avisos no Telegram quando o computador liga, desliga ou reinicia.
- Distinção entre reinicialização e desligamento.
- Detecção de desligamento incorreto (queda de energia, travamento,
  desligamento forçado).
- Fila de avisos não enviados, entregues no próximo boot.
- Avisos de "monitoramento ativado/desativado" quando o serviço é iniciado ou
  parado manualmente.
- Comandos `boot`, `shutdown`, `test` e `check`, e a opção `--dry-run`.
- Configuração em `/etc/bootbell/bootbell.conf` com mensagens personalizáveis.
- Serviço systemd rodando como usuário sem privilégios e endurecido.
- Pacote `.deb` compatível com Debian 10+, Ubuntu 18.04+ e Linux Mint 19+.

[1.0.1]: https://github.com/AprendendoLinux/bootbell/releases/tag/v1.0.1
[1.0.0]: https://github.com/AprendendoLinux/bootbell/releases/tag/v1.0.0
