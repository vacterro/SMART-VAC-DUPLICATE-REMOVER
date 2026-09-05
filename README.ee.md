# SMART VAC DUPLICATE REMOVER

**v0.0.3**

Usaldusväärne Windowsi tööriist failide dubleerimiste turvaliseks leidmiseks ja kustutamiseks: puhas liides, SHA-256 räsi kontroll ja üksikasjalik logimine.

[🤍 Toeta arendajat](https://buymeacoffee.com/vacuum34)

## Versioon
0.0.1

## Funktsioonid
- **SHA-256 räsimine**: tuvastab identsed failid täpselt, sõltumata nimetusest.
- **Kustutamine prügikasti**: kustutatud failid lähevad Windowsi prügikasti ja on taastatavad. Mujal prügikasti pole ja kinnitusaken ütleb enne lõplikku kustutamist otse välja.
- **Tühjade kaustade puhastus**: eemalda hõlpsalt allesjäänud tühjad kataloogid (valitud juurkausta ei eemaldata).
- **Üksikasjalik logimine**: kõik kirjutatakse `deleted_log.txt` faili.
- **Vaikimisi turvaline**: iga dubleerimisgrupi vähemalt üks kontrollitud koopia säilib alati; skaneerimise järel muutunud faile ei kustutata.

## Paigaldus
1. Klooni see hoidla
2. Käivita `python delete_duplicates_gui.py` Python 3.10+ abil
3. Ava GUI ja vali skaneeritav kaust

Hoidlas ei ole kaasas käivitatavat faili. Ehita see ise allpool kirjeldatud
PyInstalleri retseptiga või laadi alla märgistatud GitHubi väljalaskest, kui
selline avaldatakse.

## Kasutamine
- Vali sihtkaust
- Klõpsi **Leia dubleerimised**, et skaneerida rekursiivselt (SHA-256)
- Klõpsi **Peata otsing**, et pikk skaneerimine katkestada
- Vaata leitud dubleerimisi tulemuste puus
- Soovi korral lülita sisse kustutamise logimine
- Kustuta valitud dubleerimised (lähevad prügikasti, taastatavad)
- Kasuta **Kustuta tühjad kaustad** tühjade kataloogide eemaldamiseks

## Ehitus lähtekoodist (W2-004)
Windowsi käivitatav fail luuakse PyInstalleriga spec-failist `delete_duplicates_gui.spec`:

```
pyinstaller delete_duplicates_gui.spec
```

Väljalaskeid tuleks ehitada puhtast, lipuga märgitud commitist. Väljalaske metatabelisse tuleks kirjutada rakenduse `VERSION`, lähtekoodi commit-SHA, PyInstalleri versioon ja saadud exe SHA-256, et binaarfail on reprodutseeritav ja verifitseeritav.

## Nõuded
- Windows
- Python 3.10+ (rakendus käib lähtekoodist; hoidlas ei ole binaarfaili)

## Litsents
MIT License

## Toetus
Vigade ja funktsioonide kohta teata GitHubi hoidlas.
