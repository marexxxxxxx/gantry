import { useEffect, useRef } from "react";
import uPlot from "uplot";
import "uplot/dist/uPlot.min.css";

/** Eine Zeitreihe: Anzeigename plus [Zeit, Wert]-Punkte (Zeit in Sekunden). */
export interface Zeitreihe {
  label: string;
  points: [number, number][];
}

/** Props des Diagramms (eingefroren): Titel, Einheit und die Zeitreihen. */
export interface TimeChartProps {
  title: string;
  unit: string;
  series: Zeitreihe[];
}

/** Feste Diagrammhoehe in CSS-Pixeln; die Breite kommt vom Behaelter. */
const HOEHE = 220;

/** Farbe fuer Achsen und Gitter (entspricht den CSS-Variablen des Dark Themes). */
const ACHSE_FARBE = "#9aa3af";
const GITTER_FARBE = "#262d38";

/**
 * Bringt die Punktreihen auf eine gemeinsame x-Achse (``uPlot.AlignedData``).
 * Zeitpunkte ohne Messwert einer Reihe werden zu ``null`` (Luecke im Chart).
 */
function ausrichten(series: Zeitreihe[]): uPlot.AlignedData {
  const zeiten = Array.from(
    new Set(series.flatMap((reihe) => reihe.points.map((punkt) => punkt[0]))),
  ).sort((a, b) => a - b);
  const werte = series.map((reihe) => {
    const nachZeit = new Map(reihe.points);
    return zeiten.map((zeit) => nachZeit.get(zeit) ?? null);
  });
  const daten: uPlot.AlignedData = [zeiten, ...werte];
  return daten;
}

/**
 * uPlot-Wrapper: baut das Diagramm beim Mounten auf, aktualisiert es bei
 * neuen Daten (Effect-Abhaengigkeiten) und zerstoert es im Cleanup. Ein
 * ResizeObserver haelt die Breite am Behaelter.
 */
export default function TimeChart({ title, unit, series }: TimeChartProps) {
  const behaelter = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = behaelter.current;
    if (element === null) {
      return;
    }

    const optionen: uPlot.Options = {
      title,
      width: element.clientWidth > 0 ? element.clientWidth : 600,
      height: HOEHE,
      scales: { x: { time: true } },
      series: [
        { label: "Zeit" },
        ...series.map((reihe) => ({ label: `${reihe.label} (${unit})` })),
      ],
      axes: [
        { stroke: ACHSE_FARBE, grid: { stroke: GITTER_FARBE } },
        { stroke: ACHSE_FARBE, grid: { stroke: GITTER_FARBE } },
      ],
    };
    const diagramm = new uPlot(optionen, ausrichten(series), element);

    const beobachter = new ResizeObserver(() => {
      const breite = element.clientWidth;
      if (breite > 0 && breite !== diagramm.width) {
        diagramm.setSize({ width: breite, height: HOEHE });
      }
    });
    beobachter.observe(element);

    return () => {
      beobachter.disconnect();
      diagramm.destroy();
    };
  }, [title, unit, series]);

  return <div className="zeit-diagramm" ref={behaelter} />;
}
