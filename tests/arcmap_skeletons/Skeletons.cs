// Writes real ArcMap layer files with made-up data, one per case, so that gis-kit's ArcMap writer can take its
// skeletons from ArcObjects' own output.
// build.ps1 compiles and runs it: Skeletons.exe ROOT, where ROOT holds base_polygons.lyr, base_lines.lyr and base_points.lyr
// from make_base.py. The file names are the cases normalize.py and tests/test_package_pack_arcmap.py use.
using System;
using System.Collections.Generic;
using System.IO;
using ESRI.ArcGIS.Carto;
using ESRI.ArcGIS.Display;
using ESRI.ArcGIS.esriSystem;

class Skeletons
{
    static string root, target;

    static IColor Rgb(int r, int g, int b)
    {
        IRgbColor c = new RgbColorClass();
        c.Red = r; c.Green = g; c.Blue = b;
        return c;
    }

    static ISimpleLineSymbol Line(int r, int g, int b, double width, esriSimpleLineStyle style = esriSimpleLineStyle.esriSLSSolid)
    {
        ISimpleLineSymbol s = new SimpleLineSymbolClass();
        s.Color = Rgb(r, g, b); s.Width = width; s.Style = style;
        return s;
    }

    static ISymbol Fill(int r, int g, int b, double outline = 0.75)
    {
        ISimpleFillSymbol s = new SimpleFillSymbolClass();
        s.Style = esriSimpleFillStyle.esriSFSSolid;
        s.Color = Rgb(r, g, b);
        s.Outline = Line(90, 90, 90, outline);
        return (ISymbol)s;
    }

    static ISymbol Marker(int r, int g, int b, double size, esriSimpleMarkerStyle style = esriSimpleMarkerStyle.esriSMSCircle)
    {
        ISimpleMarkerSymbol s = new SimpleMarkerSymbolClass();
        s.Color = Rgb(r, g, b); s.Size = size; s.Style = style;
        return (ISymbol)s;
    }

    static ILayer Open(string geometry, string name)
    {
        ILayerFile file = new LayerFileClass();
        file.Open(Path.Combine(root, "base_" + geometry + ".lyr"));
        ILayer layer = file.Layer;
        layer.Name = name;
        return layer;
    }

    static void Save(ILayer layer, string name)
    {
        string path = Path.Combine(target, name + ".lyr");
        if (File.Exists(path)) File.Delete(path);
        ILayerFile file = new LayerFileClass();
        file.New(path);
        file.ReplaceContents(layer);
        file.Save();
        file.Close();
        Console.WriteLine("  " + name);
    }

    static void Simple(string geometry, string name, ISymbol symbol, Action<ILayer> more = null)
    {
        ILayer layer = Open(geometry, name);
        ISimpleRenderer r = new SimpleRendererClass();
        r.Symbol = symbol;
        ((IGeoFeatureLayer)layer).Renderer = (IFeatureRenderer)r;
        if (more != null) more(layer);
        Save(layer, name);
    }

    static readonly int[,] Palette = { { 230, 30, 40 }, { 30, 140, 200 }, { 40, 170, 90 }, { 200, 120, 20 }, { 120, 40, 180 } };
    static readonly string[] Values = { "first", "second", "third", "fourth", "fifth" };

    static ISymbol ClassSymbol(string geometry, int i)
    {
        int r = Palette[i, 0], g = Palette[i, 1], b = Palette[i, 2];
        if (geometry == "polygons") return Fill(r, g, b);
        if (geometry == "lines") return (ISymbol)Line(r, g, b, 1.5);
        return Marker(r, g, b, 7);
    }

    static void Unique(string geometry, string name, int count, bool useDefault, string[] fields = null)
    {
        ILayer layer = Open(geometry, name);
        IUniqueValueRenderer r = new UniqueValueRendererClass();
        fields = fields ?? new[] { "kind" };
        r.FieldCount = fields.Length;
        for (int i = 0; i < fields.Length; i++) r.set_Field(i, fields[i]);
        r.DefaultSymbol = ClassSymbol(geometry, 4);
        r.DefaultLabel = "others";
        r.UseDefaultSymbol = useDefault;
        for (int i = 0; i < count; i++)
            r.AddValue(Values[i], Values[i], ClassSymbol(geometry, i));
        for (int i = 0; i < count; i++)
            r.set_Label(Values[i], "label " + Values[i]);
        ((IGeoFeatureLayer)layer).Renderer = (IFeatureRenderer)r;
        Save(layer, name);
    }

    static void Breaks(string geometry, string name, int count)
    {
        ILayer layer = Open(geometry, name);
        IClassBreaksRenderer r = new ClassBreaksRendererClass();
        r.Field = "value";
        r.BreakCount = count;
        r.MinimumBreak = 1.25;
        for (int i = 0; i < count; i++)
        {
            r.set_Break(i, 10.5 * (i + 1));
            r.set_Symbol(i, ClassSymbol(geometry, i));
            r.set_Label(i, "class " + Values[i]);
        }
        ((IGeoFeatureLayer)layer).Renderer = (IFeatureRenderer)r;
        Save(layer, name);
    }

    static void Labelled(string geometry, string name, string field, double size, int r, int g, int b, bool show = true, Action<ILabelEngineLayerProperties> more = null)
    {
        ILayer layer = Open(geometry, name);
        ISimpleRenderer simple = new SimpleRendererClass();
        simple.Symbol = ClassSymbol(geometry, 0);
        IGeoFeatureLayer feature = (IGeoFeatureLayer)layer;
        feature.Renderer = (IFeatureRenderer)simple;
        feature.AnnotationProperties.Clear();
        ILabelEngineLayerProperties label = new LabelEngineLayerPropertiesClass();
        label.Expression = "[" + field + "]";
        label.IsExpressionSimple = true;
        ITextSymbol text = new TextSymbolClass();
        text.Size = size;
        text.Color = Rgb(r, g, b);
        label.Symbol = text;
        if (more != null) more(label);
        feature.AnnotationProperties.Add((IAnnotateLayerProperties)label);
        feature.DisplayAnnotation = show;
        Save(layer, name);
    }

    static void Group(string name, string[] geometries)
    {
        IGroupLayer group = new GroupLayerClass();
        ((ILayer)group).Name = name;
        foreach (string g in geometries)
        {
            ILayer child = Open(g, "child " + g);
            ISimpleRenderer simple = new SimpleRendererClass();
            simple.Symbol = ClassSymbol(g, 0);
            ((IGeoFeatureLayer)child).Renderer = (IFeatureRenderer)simple;
            group.Add(child);
        }
        Save((ILayer)group, name);
    }

    static void Try(string name, Action body)
    {
        try { body(); }
        catch (Exception e) { Console.WriteLine("FAIL " + name + " :: " + e.Message + "\n" + e.StackTrace); }
    }

    static void Main(string[] args)
    {
        root = args[0];
        target = Path.Combine(root, "gen");
        Directory.CreateDirectory(target);
        ESRI.ArcGIS.RuntimeManager.Bind(ESRI.ArcGIS.ProductCode.Desktop);
        IAoInitialize init = new AoInitializeClass();
        Console.WriteLine("license " + init.Initialize(esriLicenseProductCode.esriLicenseProductCodeAdvanced));

        Try("polygon", () => {
            Simple("polygons", "poly_base", Fill(230, 30, 40));
            Simple("polygons", "poly_named_longer", Fill(230, 30, 40));
            Simple("polygons", "poly_w2", Fill(230, 30, 40, 2.25));
            Simple("polygons", "poly_query", Fill(230, 30, 40), l => { ((IFeatureLayerDefinition)l).DefinitionExpression = "kind = 'a'"; });
            Simple("polygons", "poly_trans", Fill(230, 30, 40), l => { ((ILayerEffects)l).Transparency = 37; });
            Simple("polygons", "poly_hidden", Fill(230, 30, 40), l => { l.Visible = false; });
            ISimpleFillSymbol hollow = new SimpleFillSymbolClass();
            hollow.Style = esriSimpleFillStyle.esriSFSNull; hollow.Color = Rgb(230, 30, 40); hollow.Outline = Line(90, 90, 90, 0.75);
            Simple("polygons", "poly_hollow", (ISymbol)hollow);
            ISimpleFillSymbol bare = new SimpleFillSymbolClass();
            bare.Style = esriSimpleFillStyle.esriSFSSolid; bare.Color = Rgb(230, 30, 40); bare.Outline = Line(90, 90, 90, 0.75, esriSimpleLineStyle.esriSLSNull);
            Simple("polygons", "poly_nooutline", (ISymbol)bare);
            ISimpleFillSymbol dashed = new SimpleFillSymbolClass();
            dashed.Style = esriSimpleFillStyle.esriSFSSolid; dashed.Color = Rgb(230, 30, 40); dashed.Outline = Line(90, 90, 90, 0.75, esriSimpleLineStyle.esriSLSDash);
            Simple("polygons", "poly_dashoutline", (ISymbol)dashed);
        });
        Try("line", () => {
            Simple("lines", "line_base", (ISymbol)Line(230, 30, 40, 1.5));
            Simple("lines", "line_w4", (ISymbol)Line(230, 30, 40, 4.5));
            Simple("lines", "line_dash", (ISymbol)Line(230, 30, 40, 1.5, esriSimpleLineStyle.esriSLSDash));
            Simple("lines", "line_dot", (ISymbol)Line(230, 30, 40, 1.5, esriSimpleLineStyle.esriSLSDot));
        });
        Try("point", () => {
            Simple("points", "point_base", Marker(230, 30, 40, 7));
            Simple("points", "point_s12", Marker(230, 30, 40, 12.5));
            Simple("points", "point_square", Marker(230, 30, 40, 7, esriSimpleMarkerStyle.esriSMSSquare));
            Simple("points", "point_cross", Marker(230, 30, 40, 7, esriSimpleMarkerStyle.esriSMSCross));
            Simple("points", "point_x", Marker(230, 30, 40, 7, esriSimpleMarkerStyle.esriSMSX));
            Simple("points", "point_diamond", Marker(230, 30, 40, 7, esriSimpleMarkerStyle.esriSMSDiamond));
            ISimpleMarkerSymbol o = new SimpleMarkerSymbolClass();
            o.Color = Rgb(230, 30, 40); o.Size = 7; o.Style = esriSimpleMarkerStyle.esriSMSCircle;
            o.Outline = true; o.OutlineColor = Rgb(10, 20, 30); o.OutlineSize = 1.5;
            Simple("points", "point_outline", (ISymbol)o);
            ISimpleMarkerSymbol a = new SimpleMarkerSymbolClass();
            a.Color = Rgb(230, 30, 40); a.Size = 7; a.Style = esriSimpleMarkerStyle.esriSMSCircle; a.Angle = 33;
            Simple("points", "point_angle", (ISymbol)a);
        });
        foreach (string g in new[] { "polygons", "lines", "points" })
        {
            string geometry = g, tag = g.Substring(0, 4);
            Try("unique " + g, () => {
                for (int n = 1; n <= 4; n++) Unique(geometry, "unique_" + tag + "_" + n, n, false);
                Unique(geometry, "unique_" + tag + "_d2", 2, true);
            });
            Try("breaks " + g, () => { for (int n = 1; n <= 4; n++) Breaks(geometry, "breaks_" + tag + "_" + n, n); });
        }
        Try("labels", () => {
            Labelled("polygons", "label_poly", "kind", 9, 20, 20, 20);
            Labelled("polygons", "label_poly2", "value", 14.5, 200, 20, 20);
            Labelled("polygons", "label_off", "kind", 9, 20, 20, 20, false);
            Labelled("lines", "label_line", "kind", 9, 20, 20, 20);
            Labelled("points", "label_point", "kind", 9, 20, 20, 20);
        });
        Try("group", () => {
            Group("group_1", new[] { "polygons" }); Group("group_2", new[] { "polygons", "lines" }); Group("group_3", new[] { "polygons", "lines", "points" });
            IGroupLayer inner = new GroupLayerClass();
            ((ILayer)inner).Name = "inner";
            ILayer a = Open("polygons", "child polygons");
            ISimpleRenderer ra = new SimpleRendererClass(); ra.Symbol = ClassSymbol("polygons", 0); ((IGeoFeatureLayer)a).Renderer = (IFeatureRenderer)ra;
            inner.Add(a);
            IGroupLayer outer = new GroupLayerClass();
            ((ILayer)outer).Name = "outer";
            outer.Add((ILayer)inner);
            ILayer b = Open("lines", "child lines");
            ISimpleRenderer rb = new SimpleRendererClass(); rb.Symbol = ClassSymbol("lines", 0); ((IGeoFeatureLayer)b).Renderer = (IFeatureRenderer)rb;
            outer.Add(b);
            Save((ILayer)outer, "group_nested");
            IGroupLayer hidden = new GroupLayerClass();
            ((ILayer)hidden).Name = "hidden";
            ((ILayer)hidden).Visible = false;
            ILayer c = Open("polygons", "child polygons");
            ISimpleRenderer rc = new SimpleRendererClass(); rc.Symbol = ClassSymbol("polygons", 0); ((IGeoFeatureLayer)c).Renderer = (IFeatureRenderer)rc;
            hidden.Add(c);
            Save((ILayer)hidden, "group_hidden");
        });
        Try("label variants", () => {
            Labelled("polygons", "label_expr", "kind", 9, 20, 20, 20, true, l => { l.Expression = "[kind] & \"x\""; l.IsExpressionSimple = false; });
            Labelled("polygons", "label_font", "kind", 9, 20, 20, 20, true, l => {
                stdole.IFontDisp font = (stdole.IFontDisp)new stdole.StdFontClass();
                font.Name = "Arial"; font.Size = 9; font.Bold = true; font.Italic = true;
                ITextSymbol t = l.Symbol; t.Font = font; l.Symbol = t;
            });
            Labelled("polygons", "label_halo", "kind", 9, 20, 20, 20, true, l => {
                ITextSymbol t = l.Symbol;
                IMask mask = (IMask)t;
                mask.MaskStyle = esriMaskStyle.esriMSHalo; mask.MaskSize = 1.5;
                ISimpleFillSymbol f = new SimpleFillSymbolClass(); f.Color = Rgb(250, 240, 230); ISimpleLineSymbol n = new SimpleLineSymbolClass(); n.Style = esriSimpleLineStyle.esriSLSNull; f.Outline = n;
                mask.MaskSymbol = f;
                l.Symbol = t;
            });
        });
        init.Shutdown();
    }
}
